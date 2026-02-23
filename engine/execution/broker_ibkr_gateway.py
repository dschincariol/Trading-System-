"""
IBKR (Interactive Brokers) IB Gateway / TWS adapter using ibapi.

Institutional version:
- True delta reconciliation execution
- ALE integration
- Execution risk compatible
- Execution analytics compatible
- Position reconciliation support
- Live execution stream daemon

Supports:
- apply_latest_portfolio_orders_live()
- poll_and_log_fills()
- get_positions_snapshot()
- get_positions_live()
- run_execution_stream_daemon()

Env:
  IBKR_HOST
  IBKR_PORT
  IBKR_CLIENT_ID
  IBKR_ORDER_TIF
  IBKR_MAX_ORDERS_PER_PASS
  IBKR_SLEEP_BETWEEN_ORDERS_S
  IBKR_EQUITY_USD
"""

import os
import time
import json
import threading
from typing import Any, Dict, List, Optional, Tuple

from engine.storage import connect
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.kill_switch import execution_allowed
from engine.risk_state import get_state, set_state
from engine.execution_ledger import log_submit, log_fill
from engine.alpha_lifecycle_engine import apply_alpha_lifecycle

# Execution gate (fail-closed)
try:
    from engine.runtime.gates import execution_gate_snapshot as _execution_gate_snapshot  # type: ignore
except Exception:
    _execution_gate_snapshot = None  # type: ignore

try:
    from engine.runtime.job_registry import ALLOWED_JOBS as _ALLOWED_JOBS  # type: ignore
except Exception:
    _ALLOWED_JOBS = {}  # type: ignore

try:
    from engine.kill_switch import snapshot as _kill_switch_snapshot  # type: ignore
except Exception:
    _kill_switch_snapshot = None  # type: ignore

try:
    from engine.execution_mode import get_execution_mode as _get_execution_mode  # type: ignore
except Exception:
    _get_execution_mode = None  # type: ignore


IBKR_HOST = os.environ.get("IBKR_HOST", "127.0.0.1").strip()
IBKR_PORT = int(os.environ.get("IBKR_PORT", "7497"))
IBKR_CLIENT_ID = int(os.environ.get("IBKR_CLIENT_ID", "42"))

ORDER_TIF = os.environ.get("IBKR_ORDER_TIF", "DAY").strip().upper()
MAX_ORDERS_PER_PASS = int(os.environ.get("IBKR_MAX_ORDERS_PER_PASS", "25"))
SLEEP_BETWEEN_ORDERS_S = float(os.environ.get("IBKR_SLEEP_BETWEEN_ORDERS_S", "0.25"))


# ============================================================
# Load Latest Intent Batch
# ============================================================

def _latest_order_row(con) -> Optional[Tuple[int, int, list]]:
    from engine.portfolio_execution_intents import load_latest_execution_intents
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
        bts_i = int(bts) if bts is not None else int(time.time() * 1000)
    except Exception:
        bts_i = int(time.time() * 1000)
    return bid_i if bid_i is not None else 0, bts_i, orders


# ============================================================
# Market Price Lookup
# ============================================================

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


# ============================================================
# IB API Client Wrapper
# ============================================================

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
            self._exec_evt = threading.Event()

            self._err = []
            self._err_lock = threading.Lock()

            self._pos = []
            self._pos_lock = threading.Lock()
            self._pos_end_evt = threading.Event()

        def nextValidId(self, orderId: int):
            self._next_order_id = int(orderId)
            self._next_order_evt.set()

        def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
            with self._err_lock:
                self._err.append(
                    {"reqId": reqId, "code": errorCode, "msg": errorString}
                )

        def execDetails(self, reqId, contract, execution):
            rec = {
                "symbol": getattr(contract, "symbol", None),
                "orderId": getattr(execution, "orderId", None),
                "permId": getattr(execution, "permId", None),
                "time": getattr(execution, "time", None),
                "shares": getattr(execution, "shares", None),
                "price": getattr(execution, "price", None),
            }
            with self._exec_lock:
                self._exec.append(rec)
                self._exec_evt.set()

        def position(self, account, contract, position, avgCost):
            try:
                sym = getattr(contract, "symbol", None)
                if sym:
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
            self._pos_end_evt.set()

    app = App()
    app.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID)

    t = threading.Thread(target=app.run, daemon=True)
    t.start()

    if not app._next_order_evt.wait(timeout=10):
        raise RuntimeError("IBKR: nextValidId not received")

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


# ============================================================
# TRUE DELTA RECONCILIATION EXECUTION
# ============================================================

def apply_latest_portfolio_orders_live(
    dry_run: bool = False,
    override_orders: List[Dict[str, Any]] = None,
    override_order_id: int = None,
    override_ts_ms: int = None,
) -> Dict[str, Any]:

    # HARD EXECUTION GATE (fail-closed)
    if not bool(dry_run):
        if _execution_gate_snapshot is None:
            return {"ok": False, "status": "execution_blocked_gate_unavailable", "broker": "ibkr"}
        if _kill_switch_snapshot is None or _get_execution_mode is None:
            return {"ok": False, "status": "execution_blocked_gate_providers_missing", "broker": "ibkr"}

        # Build minimal job list from job_locks
        def _jobs_from_db_snapshot() -> List[Dict[str, Any]]:
            try:
                max_stale_s = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))
            except Exception:
                max_stale_s = 180.0
            now_ms = int(time.time() * 1000)

            try:
                c = connect(readonly=True)
                try:
                    rows = c.execute("SELECT job_name, heartbeat_ts_ms FROM job_locks").fetchall()
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass
            except Exception:
                rows = []

            out: List[Dict[str, Any]] = []
            for r in rows or []:
                try:
                    name = str(r[0] or "")
                    hb = int(r[1] or 0)
                    running = (now_ms - hb) <= int(max_stale_s * 1000.0)

                    mode = ""
                    try:
                        spec = _ALLOWED_JOBS.get(name)
                        if isinstance(spec, (list, tuple)) and len(spec) >= 2:
                            mode = str(spec[1] or "")
                    except Exception:
                        mode = ""

                    out.append({"name": name, "running": bool(running), "mode": mode})
                except Exception:
                    continue

            return out

        gate = _execution_gate_snapshot(
            get_jobs=_jobs_from_db_snapshot,
            get_kill_switches=lambda: (_kill_switch_snapshot() or {}),
            get_execution_mode=lambda: (_get_execution_mode() or {}),
        )

        if not bool(gate.get("ok")):
            return {"ok": False, "status": "execution_blocked", "broker": "ibkr", "gate": gate}

    con = connect()
    try:
        if override_orders is not None:
            order_id = override_order_id
            ts_ms = override_ts_ms or int(time.time() * 1000)
            orders = list(override_orders or [])
        else:
            latest = _latest_order_row(con)
            if not latest:
                return {"ok": True, "status": "no_orders", "broker": "ibkr"}
            order_id, ts_ms, orders = latest

        # ALE
        orders_ale, _ = apply_alpha_lifecycle(
            con=con,
            portfolio_orders_id=order_id,
            portfolio_ts_ms=int(ts_ms),
            orders=orders,
        )

        last_applied = get_state("ibkr_last_portfolio_orders_id", "0")
        if order_id is not None:
            try:
                if int(last_applied) >= int(order_id):
                    return {"ok": True, "status": "already_applied", "broker": "ibkr"}
            except Exception:
                pass

        allow0, _, _ = execution_allowed(con=con, symbol=None, regime=None)
        if not allow0:
            return {"ok": False, "status": "blocked_kill_switch", "broker": "ibkr"}

        eq = float(os.environ.get("IBKR_EQUITY_USD", "0") or 0.0)
        if eq <= 0:
            return {"ok": False, "status": "missing_equity", "broker": "ibkr"}

        if dry_run:
            return {"ok": True, "status": "dry_run_preview", "orders": orders_ale}

        # ----------------------------------------
        # Pull LIVE POSITIONS
        # ----------------------------------------
        live_positions = {p["symbol"]: p["qty"] for p in get_positions_live()}

        app = _connect_ib()
        submitted = []
        n = 0

        for o in orders_ale[: int(MAX_ORDERS_PER_PASS)]:
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

            target_qty = (to_w * eq) / px
            if to_side == "SHORT":
                target_qty = -abs(target_qty)
            elif to_side == "LONG":
                target_qty = abs(target_qty)
            else:
                target_qty = 0.0

            current_qty = float(live_positions.get(symbol, 0.0))
            delta = float(target_qty - current_qty)

            if abs(delta) < 1e-6:
                continue

            contract = _mk_stock_contract(symbol)
            order = _mk_market_order(delta)

            oid = int(app._next_order_id)
            app._next_order_id += 1
            app.placeOrder(oid, contract, order)

            log_submit(
                client_order_id=f"pf_{order_id}_{symbol}",
                broker="ibkr",
                symbol=symbol,
                qty=float(delta),
                submit_ts_ms=int(time.time() * 1000),
                ref_px=float(px),
                broker_order_id=str(oid),
                portfolio_orders_id=order_id,
                source_alert_id=(int(o.get("source_alert_id")) if isinstance(o, dict) and o.get("source_alert_id") is not None else None),
                extra=o,
            )

            submitted.append({"symbol": symbol, "delta_qty": delta})
            n += 1
            time.sleep(max(0.0, float(SLEEP_BETWEEN_ORDERS_S)))

        if order_id is not None:
            set_state("ibkr_last_portfolio_orders_id", str(int(order_id)))

        return {"ok": True, "status": "applied", "submitted_n": n, "broker": "ibkr"}

    finally:
        con.close()


# ============================================================
# Positions
# ============================================================

def get_positions_snapshot(timeout_s: float = 10.0) -> Dict[str, float]:
    try:
        rows = get_positions_live(timeout_s)
        return {r["symbol"]: r["qty"] for r in rows}
    except Exception:
        return {}


def get_positions_live(timeout_s: float = 8.0) -> List[Dict[str, Any]]:
    app = _connect_ib()
    try:
        with app._pos_lock:
            app._pos = []
        app._pos_end_evt.clear()

        app.reqPositions()
        ok = app._pos_end_evt.wait(timeout=float(timeout_s))
        app.cancelPositions()

        if not ok:
            raise RuntimeError("IBKR: positions timeout")

        with app._pos_lock:
            return list(app._pos or [])
    finally:
        app.disconnect()


# ============================================================
# Poll Stub
# ============================================================

def poll_and_log_fills(after_ts_ms: int) -> Dict[str, Any]:
    return {"ok": True, "fills_logged": 0, "status": "ibkr_poll_stub"}


# ============================================================
# Execution Stream Daemon
# ============================================================

def run_execution_stream_daemon(poll_sleep_s: float = 1.0) -> None:
    app = _connect_ib()
    con = connect()

    from ibapi.execution import ExecutionFilter
    filt = ExecutionFilter()

    try:
        while True:
            app._exec_evt.clear()
            app.reqExecutions(0, filt)
            app._exec_evt.wait(timeout=5.0)

            with app._exec_lock:
                rows = list(app._exec or [])
                app._exec = []

            for r in rows:
                try:
                    log_fill(
                        client_order_id=str(r.get("orderId")),
                        broker="ibkr",
                        symbol=str(r.get("symbol")),
                        qty=float(r.get("shares") or 0.0),
                        fill_px=float(r.get("price") or 0.0),
                        fill_ts_ms=int(time.time() * 1000),
                        broker_fill_id=str(r.get("permId") or ""),
                        extra=r,
                    )
                except Exception:
                    continue

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
