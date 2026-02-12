# dev_core/broker_ibkr_gateway.py
"""
IBKR (Interactive Brokers) IB Gateway / TWS adapter using ibapi.

Supports:
- apply_latest_portfolio_orders_live(dry_run=False, override_orders=..., override_order_id=..., override_ts_ms=...)
- poll_and_log_fills(after_ts_ms)  (stub-safe)
- get_positions_snapshot(timeout_s=10.0)  -> {SYM: qty}
- get_positions_live(timeout_s=8.0)       -> [{"symbol","qty","avg_cost"}, ...]
- run_execution_stream_daemon(poll_sleep_s=1.0)  (long-lived exec subscriber; best-effort)

Env:
  IBKR_HOST=127.0.0.1
  IBKR_PORT=7497        (paper) or 7496 (live) depending on Gateway/TWS config
  IBKR_CLIENT_ID=42

  IBKR_ORDER_TIF=DAY
  IBKR_MAX_ORDERS_PER_PASS=25
  IBKR_SLEEP_BETWEEN_ORDERS_S=0.25

  IBKR_EQUITY_USD=...   (required for sizing safety)
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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _latest_order_row(con) -> Optional[Tuple[int, int, list]]:
    """
    Back-compat shim (orders_json table no longer required).
    Returns (batch_id, batch_ts_ms, intents[])
    """
    from dev_core.portfolio_execution_intents import load_latest_execution_intents

    b = load_latest_execution_intents(con)
    orders = list(b.get("intents") or [])
    if not orders:
        return None

    bid = b.get("batch_id")
    bts = b.get("batch_ts_ms")

    try:
        bid_i = int(bid) if bid is not None else None
    except Exception:
        bid_i = None

    try:
        bts_i = int(bts) if bts is not None else _now_ms()
    except Exception:
        bts_i = _now_ms()

    return (bid_i if bid_i is not None else 0), bts_i, orders


def _price_at_or_before(con, symbol: str, ts_ms: int) -> Optional[float]:
    """
    Best-effort DB mark price (expects prices(px, ts_ms) schema).
    """
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
    s = str(to_side).upper()
    if s == "SHORT":
        return -abs(qty)
    if s == "LONG":
        return abs(qty)
    return 0.0


# ----------------------------
# IB API Client Wrapper
# ----------------------------
def _connect_ib():
    try:
        from ibapi.client import EClient
        from ibapi.wrapper import EWrapper
        from ibapi.contract import Contract
        from ibapi.order import Order
    except Exception:
        raise RuntimeError("ibapi not installed or not available in environment")

    class App(EWrapper, EClient):
        def __init__(self):
            EClient.__init__(self, self)

            self._next_order_id = None
            self._next_order_evt = threading.Event()

            # Executions buffer
            self._exec = []
            self._exec_lock = threading.Lock()
            self._exec_evt = threading.Event()
            self._exec_end_evt = threading.Event()

            # Errors buffer
            self._err = []
            self._err_lock = threading.Lock()

            # Positions (pre-live reconciliation)
            self._pos = []
            self._pos_lock = threading.Lock()
            self._pos_end_evt = threading.Event()

        def nextValidId(self, orderId: int):
            try:
                self._next_order_id = int(orderId)
                self._next_order_evt.set()
            except Exception:
                pass

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            try:
                with self._err_lock:
                    self._err.append(
                        {"reqId": reqId, "code": errorCode, "msg": errorString, "adv": advancedOrderRejectJson}
                    )
            except Exception:
                pass

        def execDetails(self, reqId, contract, execution):
            try:
                rec = {
                    "symbol": getattr(contract, "symbol", None),
                    "orderId": getattr(execution, "orderId", None),
                    "permId": getattr(execution, "permId", None),
                    "clientId": getattr(execution, "clientId", None),
                    "time": getattr(execution, "time", None),
                    "shares": getattr(execution, "shares", None),
                    "price": getattr(execution, "price", None),
                }
                with self._exec_lock:
                    self._exec.append(rec)
                    self._exec_evt.set()
            except Exception:
                pass

        def execDetailsEnd(self, reqId: int):
            try:
                self._exec_end_evt.set()
            except Exception:
                pass

        def position(self, account, contract, position, avgCost):
            try:
                sym = getattr(contract, "symbol", None)
                if sym is None:
                    return
                with self._pos_lock:
                    self._pos.append(
                        {
                            "symbol": str(sym).upper().strip(),
                            "qty": float(position or 0.0),
                            "avg_cost": float(avgCost or 0.0),
                        }
                    )
            except Exception:
                pass

        def positionEnd(self):
            try:
                self._pos_end_evt.set()
            except Exception:
                pass

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


def apply_latest_portfolio_orders_live(
    dry_run: bool = False,
    override_orders: List[Dict[str, Any]] = None,
    override_order_id: int = None,
    override_ts_ms: int = None,
) -> Dict[str, Any]:
    """
    Places MARKET orders for each (symbol, to_side, to_weight) intent.
    Sizing uses IBKR_EQUITY_USD env as a safety cap (required).
    """
    con = connect()
    try:
        if override_orders is not None:
            order_id = int(override_order_id) if override_order_id is not None else None
            ts_ms = int(override_ts_ms) if override_ts_ms is not None else _now_ms()
            orders = list(override_orders or [])
        else:
            latest = _latest_order_row(con)
            if not latest:
                return {"ok": True, "status": "no_orders", "broker": "ibkr"}
            order_id, ts_ms, orders = latest

        # idempotency guard via risk_state
        last_applied = get_state("ibkr_last_portfolio_orders_id", "0")
        if order_id is not None:
            try:
                if int(last_applied) >= int(order_id):
                    return {"ok": True, "status": "already_applied", "order_id": int(order_id), "broker": "ibkr"}
            except Exception:
                pass

        # kill switch global
        allow0, _, _ = execution_allowed(con=con, symbol=None, regime=None)
        if not allow0:
            return {"ok": False, "status": "blocked_kill_switch_global", "order_id": order_id, "broker": "ibkr"}

        # safety equity cap (required)
        eq = float(os.environ.get("IBKR_EQUITY_USD", "0") or 0.0)
        if eq <= 0:
            return {
                "ok": False,
                "status": "missing_equity",
                "hint": "set IBKR_EQUITY_USD env to a positive number",
                "order_id": order_id,
                "broker": "ibkr",
            }

        if dry_run:
            return {
                "ok": True,
                "status": "dry_run_preview",
                "order_id": (int(order_id) if order_id is not None else None),
                "ts_ms": int(ts_ms),
                "orders": orders,
                "equity_env": float(eq),
                "broker": "ibkr",
            }

        app = _connect_ib()
        try:
            submitted: List[Dict[str, Any]] = []
            n = 0

            for o in (orders or [])[: int(MAX_ORDERS_PER_PASS)]:
                symbol = str(o.get("symbol") or "").strip().upper()
                if not symbol:
                    continue

                allow_sym, _, _ = execution_allowed(con=con, symbol=symbol, regime=None)
                if not allow_sym:
                    continue

                to_side = str(o.get("to_side") or "FLAT").upper().strip()
                try:
                    to_w = float(o.get("to_weight") or 0.0)
                except Exception:
                    to_w = 0.0

                px = _price_at_or_before(con, symbol, int(ts_ms))
                if px is None or float(px) <= 0:
                    continue

                tgt = _target_qty(eq=float(eq), px=float(px), to_side=to_side, to_w=float(to_w))

                # NOTE: minimal adapter: submits target as delta (no position reconciliation here)
                delta = float(tgt)
                if abs(delta) < 1e-6:
                    continue

                client_oid = f"pf_{int(order_id) if order_id is not None else 0}_{symbol}"

                contract = _mk_stock_contract(symbol)
                order = _mk_market_order(delta)

                ib_order_id = int(app._next_order_id)
                app._next_order_id += 1

                app.placeOrder(ib_order_id, contract, order)

                # ledger submit (best-effort)
                try:
                    log_submit(
                        client_order_id=client_oid,
                        broker="ibkr",
                        symbol=symbol,
                        qty=float(delta),
                        submit_ts_ms=_now_ms(),
                        ref_px=float(px),
                        broker_order_id=str(ib_order_id),
                        portfolio_orders_id=(int(order_id) if order_id is not None else None),
                        source_alert_id=(int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                        extra={"to_side": to_side, "to_weight": float(to_w), "equity_env": float(eq)},
                    )
                except Exception:
                    pass

                submitted.append(
                    {
                        "symbol": symbol,
                        "delta_qty": float(delta),
                        "client_order_id": client_oid,
                        "ib_order_id": int(ib_order_id),
                    }
                )
                n += 1
                time.sleep(max(0.0, float(SLEEP_BETWEEN_ORDERS_S)))

            if order_id is not None:
                try:
                    set_state("ibkr_last_portfolio_orders_id", str(int(order_id)))
                except Exception:
                    pass

            return {
                "ok": True,
                "status": "applied",
                "order_id": (int(order_id) if order_id is not None else None),
                "submitted_n": int(n),
                "submitted": submitted,
                "broker": "ibkr",
            }
        finally:
            try:
                app.disconnect()
            except Exception:
                pass

    finally:
        con.close()


def poll_and_log_fills(after_ts_ms: int) -> Dict[str, Any]:
    """
    Minimal polling stub for IBKR fills.
    Real IBKR fill capture is event-driven via execDetails; for a full implementation,
    run run_execution_stream_daemon() as a long-lived process.
    """
    return {"ok": True, "fills_logged": 0, "after_ts_ms": int(after_ts_ms), "status": "ibkr_poll_stub", "broker": "ibkr"}


def get_positions_snapshot(timeout_s: float = 10.0) -> Dict[str, float]:
    """
    Backward-compatible helper.

    Returns {symbol: qty} best-effort.
    """
    out: Dict[str, float] = {}
    try:
        rows = get_positions_live(timeout_s=float(timeout_s))
        for r in rows or []:
            try:
                sym = str(r.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                out[sym] = float(r.get("qty") or 0.0)
            except Exception:
                continue
    except Exception:
        return {}
    return out


def get_positions_live(timeout_s: float = 8.0) -> List[Dict[str, Any]]:
    """
    Pulls current IBKR positions via reqPositions().

    Returns: [{"symbol": "SPY", "qty": 10.0, "avg_cost": 123.45}, ...]

    Notes:
    - Requires IB Gateway/TWS API enabled.
    - Best-effort; raises on connection failure.
    """
    app = _connect_ib()
    try:
        # reset buffers
        try:
            with app._pos_lock:
                app._pos = []
            app._pos_end_evt.clear()
        except Exception:
            pass

        app.reqPositions()
        ok = app._pos_end_evt.wait(timeout=float(timeout_s))
        try:
            app.cancelPositions()
        except Exception:
            pass

        if not ok:
            raise RuntimeError("IBKR: positions timeout (positionEnd not received)")

        with app._pos_lock:
            out = list(app._pos or [])

        # normalize
        norm: List[Dict[str, Any]] = []
        for p in out:
            try:
                sym = str(p.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                q = float(p.get("qty") or 0.0)
                ac = float(p.get("avg_cost") or 0.0)
                norm.append({"symbol": sym, "qty": q, "avg_cost": ac})
            except Exception:
                continue

        return norm
    finally:
        try:
            app.disconnect()
        except Exception:
            pass


# ============================================================
# Live Execution Subscriber (Best-effort)
# ============================================================

def run_execution_stream_daemon(poll_sleep_s: float = 1.0) -> None:
    """
    Long-lived IBKR execution subscriber.

    - Calls reqExecutions() periodically
    - Writes fills to execution_ledger (best-effort)
    - Best-effort updates broker_order_state (if exists)
    """
    from ibapi.execution import ExecutionFilter

    app = _connect_ib()
    con = connect()

    try:
        # best-effort ensure order-state table exists (matches your sim schema)
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_order_state (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  source_order_id INTEGER,
                  symbol TEXT NOT NULL,
                  state TEXT NOT NULL,
                  created_ts_ms INTEGER NOT NULL,
                  updated_ts_ms INTEGER NOT NULL,
                  ttl_ms INTEGER,
                  meta_json TEXT
                )
                """
            )
            con.commit()
        except Exception:
            pass

        filt = ExecutionFilter()

        while True:
            try:
                # reset execution buffers
                try:
                    with app._exec_lock:
                        app._exec = []
                    app._exec_evt.clear()
                    app._exec_end_evt.clear()
                except Exception:
                    pass

                # request executions (reqId=0 is acceptable in many setups)
                try:
                    app.reqExecutions(0, filt)
                except Exception:
                    time.sleep(5.0)
                    continue

                # wait for at least something OR end marker OR timeout
                # (different gateway versions behave differently)
                app._exec_evt.wait(timeout=2.0)
                app._exec_end_evt.wait(timeout=5.0)

                rows: List[Dict[str, Any]] = []
                try:
                    with app._exec_lock:
                        rows = list(app._exec or [])
                except Exception:
                    rows = []

                # optional: cancel execution request (best-effort)
                try:
                    app.cancelExecutions(0)
                except Exception:
                    pass

                for r in rows or []:
                    try:
                        symbol = str(r.get("symbol") or "").upper().strip()
                        shares = float(r.get("shares") or 0.0)
                        price = float(r.get("price") or 0.0)
                        perm_id = r.get("permId")
                        order_id = r.get("orderId")
                        ts_ms = _now_ms()

                        if not symbol or abs(shares) <= 0 or price <= 0:
                            continue

                        # execution ledger fill (best-effort; uses common signature seen elsewhere)
                        try:
                            log_fill(
                                client_order_id=str(order_id) if order_id is not None else f"ibkr_{symbol}",
                                fill_ts_ms=int(ts_ms),
                                fill_qty=float(shares),
                                fill_px=float(price),
                                fees=None,
                                liquidity="ibkr",
                                raw=r,
                            )
                        except Exception:
                            pass

                        # best-effort update broker_order_state (symbol-based)
                        try:
                            con.execute(
                                """
                                UPDATE broker_order_state
                                SET state=?, updated_ts_ms=?, meta_json=?
                                WHERE symbol=? AND state IN ('SUBMITTING','SUBMITTED','PENDING')
                                """,
                                (
                                    "FILLED",
                                    int(ts_ms),
                                    json.dumps(
                                        {
                                            "fill_qty": float(shares),
                                            "fill_px": float(price),
                                            "permId": (str(perm_id) if perm_id is not None else None),
                                            "orderId": (int(order_id) if order_id is not None else None),
                                        },
                                        separators=(",", ":"),
                                        sort_keys=True,
                                    ),
                                    symbol,
                                ),
                            )
                            con.commit()
                        except Exception:
                            pass

                    except Exception:
                        continue

            except Exception:
                time.sleep(5.0)

            time.sleep(max(0.1, float(poll_sleep_s)))

    finally:
        try:
            app.disconnect()
        except Exception:
            pass
        try:
            con.close()
        except Exception:
            pass
