# dev_core/broker_alpaca_rest.py
"""
Alpaca Trading API v2 (REST) adapter.

Env:
  ALPACA_BASE_URL=https://paper-api.alpaca.markets
  ALPACA_KEY_ID=...
  ALPACA_SECRET_KEY=...

Optional execution knobs:
  ALPACA_ORDER_TIF=day
  ALPACA_ORDER_TYPE=market
  ALPACA_MAX_ORDERS_PER_PASS=25
  ALPACA_SLEEP_BETWEEN_ORDERS_S=0.25

Limit microstructure knobs:
  ALPACA_LIMIT_OFFSET_BPS_PASSIVE=5.0
  ALPACA_LIMIT_OFFSET_BPS_NEUTRAL=2.0
  ALPACA_LIMIT_OFFSET_BPS_AGGRESSIVE=0.5
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from dev_core.execution_ledger import log_submit, log_fill
from dev_core.storage import connect
from dev_core.kill_switch import execution_allowed
from dev_core.risk_state import get_state, set_state
from dev_core.execution_microstructure import record_open_order


BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets").strip()
KEY_ID = os.environ.get("ALPACA_KEY_ID", "").strip()
SECRET = os.environ.get("ALPACA_SECRET_KEY", "").strip()

ORDER_TIF = os.environ.get("ALPACA_ORDER_TIF", "day").strip()
ORDER_TYPE = os.environ.get("ALPACA_ORDER_TYPE", "market").strip()
MAX_ORDERS_PER_PASS = int(os.environ.get("ALPACA_MAX_ORDERS_PER_PASS", "25"))
SLEEP_BETWEEN_ORDERS_S = float(os.environ.get("ALPACA_SLEEP_BETWEEN_ORDERS_S", "0.25"))

LIM_OFF_BPS_PASSIVE = float(os.environ.get("ALPACA_LIMIT_OFFSET_BPS_PASSIVE", "5.0"))
LIM_OFF_BPS_NEUTRAL = float(os.environ.get("ALPACA_LIMIT_OFFSET_BPS_NEUTRAL", "2.0"))
LIM_OFF_BPS_AGGR = float(os.environ.get("ALPACA_LIMIT_OFFSET_BPS_AGGRESSIVE", "0.5"))


def _headers() -> Dict[str, str]:
    return {
        "APCA-API-KEY-ID": KEY_ID,
        "APCA-API-SECRET-KEY": SECRET,
        "Content-Type": "application/json",
    }


def _req(method: str, path: str, payload: Optional[dict] = None) -> Any:
    if not KEY_ID or not SECRET:
        raise RuntimeError("alpaca credentials missing: ALPACA_KEY_ID / ALPACA_SECRET_KEY")
    url = BASE_URL.rstrip("/") + path
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    r = urllib.request.Request(url, data=data, headers=_headers(), method=method.upper())
    with urllib.request.urlopen(r, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def get_account() -> Dict[str, Any]:
    return _req("GET", "/v2/account")


def get_positions() -> List[Dict[str, Any]]:
    res = _req("GET", "/v2/positions")
    return list(res or [])


def get_order(order_id: str) -> Dict[str, Any]:
    return _req("GET", f"/v2/orders/{str(order_id)}")


def cancel_order(order_id: str) -> Dict[str, Any]:
    return _req("DELETE", f"/v2/orders/{str(order_id)}")


def list_orders_after(after_ts_ms: int, status: str = "all", limit: int = 500) -> List[Dict[str, Any]]:
    dt = datetime.fromtimestamp(float(after_ts_ms) / 1000.0, tz=timezone.utc)
    after = dt.isoformat().replace("+00:00", "Z")
    path = f"/v2/orders?status={status}&after={after}&direction=asc&limit={int(limit)}"
    res = _req("GET", path)
    return list(res or [])


def _latest_order_row(con) -> Optional[Tuple[int, int, list]]:
    """
    Back-compat shim (orders_json table no longer required).
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
        bts_i = int(bts) if bts is not None else int(time.time() * 1000)
    except Exception:
        bts_i = int(time.time() * 1000)
    return bid_i if bid_i is not None else 0, bts_i, orders


def _price_at_or_before(con, symbol: str, ts_ms: int) -> Optional[float]:
    try:
        r = con.execute(
            """
            SELECT price
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


def _alpaca_pos_map(positions: List[Dict[str, Any]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for p in positions or []:
        try:
            sym = str(p.get("symbol") or "").upper().strip()
            qty = float(p.get("qty") or 0.0)
            if sym:
                out[sym] = qty
        except Exception:
            continue
    return out


def _load_portfolio_state_positions(con) -> Dict[str, float]:
    rows = con.execute(
        """
        SELECT symbol, side, weight
        FROM portfolio_state
        """
    ).fetchall()

    acct = get_account()
    eq = float(acct.get("equity") or 0.0)
    out: Dict[str, float] = {}

    now_ms = int(time.time() * 1000)
    for sym, side, w in rows or []:
        px = _price_at_or_before(con, sym, now_ms)
        if px is None or px <= 0:
            continue
        qty = (float(w) * eq) / float(px)
        if side == "SHORT":
            qty = -abs(qty)
        elif side == "LONG":
            qty = abs(qty)
        else:
            qty = 0.0
        out[str(sym)] = float(qty)

    return out


def _submit_market_order(symbol: str, qty: float, client_oid: str) -> Dict[str, Any]:
    side = "buy" if qty > 0 else "sell"
    payload = {
        "symbol": str(symbol),
        "qty": str(abs(qty)),
        "side": side,
        "type": "market",
        "time_in_force": str(ORDER_TIF),
        "client_order_id": str(client_oid),
    }
    return _req("POST", "/v2/orders", payload)


def _submit_limit_order(symbol: str, qty: float, limit_price: float, client_oid: str) -> Dict[str, Any]:
    side = "buy" if qty > 0 else "sell"
    payload = {
        "symbol": str(symbol),
        "qty": str(abs(qty)),
        "side": side,
        "type": "limit",
        "time_in_force": str(ORDER_TIF),
        "limit_price": str(float(limit_price)),
        "client_order_id": str(client_oid),
    }
    return _req("POST", "/v2/orders", payload)


def submit_limit_order(symbol: str, qty: float, limit_price: float, client_oid: str) -> Dict[str, Any]:
    return _submit_limit_order(symbol=symbol, qty=qty, limit_price=limit_price, client_oid=client_oid)


def _limit_from_px(px: float, qty: float, aggressiveness: str) -> float:
    a = str(aggressiveness or "").upper().strip()
    if a == "PASSIVE":
        off = float(LIM_OFF_BPS_PASSIVE)
    elif a == "NEUTRAL":
        off = float(LIM_OFF_BPS_NEUTRAL)
    else:
        off = float(LIM_OFF_BPS_AGGR)

    if float(qty) > 0:
        return float(px) * (1.0 - (off / 10000.0))
    return float(px) * (1.0 + (off / 10000.0))


def apply_latest_portfolio_orders_live(
    dry_run: bool = False,
    override_orders: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if not KEY_ID or not SECRET:
        return {"ok": False, "status": "missing_credentials"}

    con = connect()
    try:

        if override_orders is not None:
            order_id = None
            ts_ms = int(time.time() * 1000)
            orders = list(override_orders or [])
        else:
            latest = _latest_order_row(con)
            if not latest:
                return {"ok": True, "status": "no_orders", "broker": "ibkr"}
            order_id, ts_ms, orders = latest

        # -----------------------------
        # ALE/EPE integration note:
        # - EPE should already have TTL-filtered these intents before routing.
        # - broker_sim still enforces per-order TTL locally (defense in depth).
        # -----------------------------
        ale_meta = {"ok": True, "note": "ale_applied_upstream_or_ttl_guard_local"}

        # idempotency guard via risk_state (only for real portfolio_orders rows)
        if order_id is not None:
            last_applied = get_state("alpaca_last_portfolio_orders_id", "0")
            try:
                if int(last_applied) >= int(order_id):
                    return {"ok": True, "status": "already_applied", "order_id": int(order_id)}
            except Exception:
                pass

        allow0, _, _ = execution_allowed(con=con, symbol=None, regime=None)
        if not allow0:
            return {"ok": False, "status": "blocked_kill_switch_global", "order_id": order_id}

        acct = get_account()
        eq = float(acct.get("equity") or 0.0)
        if eq <= 0:
            return {"ok": False, "status": "nonpositive_equity", "equity": eq, "order_id": order_id}

        pos = _alpaca_pos_map(get_positions())

        if dry_run:
            return {
                "ok": True,
                "status": "dry_run_preview",
                "order_id": int(order_id),
                "positions": pos,
                "orders": orders,
                "ale": ale_meta,
                "ale": ale_meta,
            }

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

            target_qty = (to_w * eq) / float(px)
            if to_side == "SHORT":
                target_qty = -abs(target_qty)
            elif to_side == "LONG":
                target_qty = abs(target_qty)
            else:
                target_qty = 0.0

            cur_qty = float(pos.get(symbol, 0.0))
            delta = float(target_qty) - float(cur_qty)
            if abs(delta) < 1e-6:
                continue

            order_type = str(o.get("order_type") or ORDER_TYPE).upper().strip()
            aggressiveness = str(o.get("aggressiveness") or "").upper().strip()
            cancel_replace = bool(o.get("cancel_replace", False))
            max_reprice_attempts = int(o.get("max_reprice_attempts") or 0)

            client_oid = f"pf_{order_id}_{symbol}"

            if order_type == "LIMIT":
                limit_px = _limit_from_px(float(px), float(delta), aggressiveness)
                res = _submit_limit_order(symbol=symbol, qty=delta, limit_price=float(limit_px), client_oid=client_oid)

                broker_order_id = None
                try:
                    broker_order_id = str((res or {}).get("id") or "") or None
                    log_submit(
                        client_order_id=client_oid,
                        broker="alpaca",
                        symbol=symbol,
                        qty=float(delta),
                        submit_ts_ms=int(time.time() * 1000),
                        ref_px=float(limit_px),
                        broker_order_id=broker_order_id,
                        portfolio_orders_id=int(order_id) if order_id is not None else None,
                        source_alert_id=int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None,
                        extra={"order_type": "LIMIT", "aggressiveness": aggressiveness, "equity": float(eq)},
                    )
                except Exception:
                    pass

                if cancel_replace and max_reprice_attempts > 0:
                    try:
                        record_open_order(
                            broker="alpaca",
                            symbol=symbol,
                            qty=float(delta),
                            order_type="LIMIT",
                            aggressiveness=aggressiveness,
                            limit_px=float(limit_px),
                            client_order_id=client_oid,
                            broker_order_id=broker_order_id,
                            max_attempts=int(max_reprice_attempts),
                            portfolio_orders_id=int(order_id) if order_id is not None else None,
                            source_alert_id=int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None,
                            meta={"ref_px": float(px), "ts_ms": int(ts_ms)},
                        )
                    except Exception:
                        pass

            else:
                # MARKET fallback
                res = _submit_market_order(symbol=symbol, qty=delta, client_oid=client_oid)

                try:
                    broker_order_id = str((res or {}).get("id") or "") or None
                    log_submit(
                        client_order_id=client_oid,
                        broker="alpaca",
                        symbol=symbol,
                        qty=float(delta),
                        submit_ts_ms=int(time.time() * 1000),
                        ref_px=float(px),
                        broker_order_id=broker_order_id,
                        portfolio_orders_id=int(order_id) if order_id is not None else None,
                        source_alert_id=int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None,
                        extra={"order_type": "MARKET", "equity": float(eq)},
                    )
                except Exception:
                    pass

            submitted.append({"symbol": symbol, "delta_qty": delta, "client_order_id": client_oid, "resp": res})
            n += 1
            time.sleep(max(0.0, float(SLEEP_BETWEEN_ORDERS_S)))

        if order_id is not None:
            try:
                set_state("alpaca_last_portfolio_orders_id", str(int(order_id)))
            except Exception:
                pass

        return {
            "ok": True,
            "broker": "alpaca",
            "status": "applied",
            "order_id": order_id,
            "submitted_n": int(n),
            "submitted": submitted,
        }

    finally:
        con.close()


def poll_and_log_fills(after_ts_ms: int) -> Dict[str, Any]:
    """
    Poll Alpaca orders after a given timestamp and log fills into execution_ledger.
    Only logs orders that have a client_order_id.
    """
    n = 0
    orders = list_orders_after(after_ts_ms=int(after_ts_ms), status="all", limit=500)
    for o in orders or []:
        try:
            cid = str(o.get("client_order_id") or "").strip()
            if not cid:
                continue

            filled_qty = float(o.get("filled_qty") or 0.0)
            filled_avg = o.get("filled_avg_price")
            if filled_avg is None:
                continue
            filled_avg = float(filled_avg)

            if filled_qty <= 0 or filled_avg <= 0:
                continue

            ts = o.get("filled_at") or o.get("updated_at") or o.get("created_at")
            fill_ts_ms = int(time.time() * 1000)
            try:
                if ts:
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                    fill_ts_ms = int(dt.timestamp() * 1000)
            except Exception:
                fill_ts_ms = int(time.time() * 1000)

            log_fill(
                client_order_id=cid,
                fill_ts_ms=int(fill_ts_ms),
                fill_qty=float(filled_qty),
                fill_px=float(filled_avg),
                fees=None,
                liquidity=str(o.get("order_class") or ""),
                raw=o,
            )
            n += 1
        except Exception:
            continue

    return {"ok": True, "fills_logged": int(n), "polled": len(orders or [])}
