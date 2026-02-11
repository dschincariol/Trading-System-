# dev_core/broker_router.py
"""
Broker router + failover:

- sim   -> dev_core.broker_sim.apply_new_portfolio_orders
- alpaca-> dev_core.broker_alpaca_rest.apply_latest_portfolio_orders_live
- ibkr  -> dev_core.broker_ibkr_gateway.apply_latest_portfolio_orders_live

Failover:
  BROKER_FAILOVER="ibkr,alpaca" (or "alpaca,ibkr", etc)
  If the first broker errors or returns ok=False, the router tries the next broker.
"""

import os
import time
from typing import Any, Dict, List, Optional

from dev_core.broker_sim import apply_new_portfolio_orders as _sim_apply
from dev_core.broker_alpaca_rest import apply_latest_portfolio_orders_live as _alpaca_apply

try:
    from dev_core.broker_ibkr_gateway import apply_latest_portfolio_orders_live as _ibkr_apply
except Exception:
    _ibkr_apply = None


def _parse_failover_chain() -> List[str]:
    raw = (os.environ.get("BROKER_FAILOVER", "") or "").strip()
    if raw:
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if parts:
            return parts

    name = str(os.environ.get("BROKER_NAME", "sim") or "sim").lower().strip()
    return [name] if name else ["sim"]


def _call_adapter(fn, *, dry_run: bool, override_orders: Optional[List[dict]]):
    if override_orders is None:
        return fn(dry_run=bool(dry_run))

    try:
        return fn(dry_run=bool(dry_run), override_orders=override_orders)
    except TypeError:
        return fn(dry_run=bool(dry_run))


def _apply_one(name: str, *, dry_run: bool, override_orders: Optional[List[dict]] = None) -> Dict[str, Any]:
    name = (name or "").lower().strip()

    if name in ("sim", "paper", "sandbox"):
        return _call_adapter(_sim_apply, dry_run=bool(dry_run), override_orders=override_orders)

    if name in ("alpaca", "alpaca_rest"):
        return _call_adapter(_alpaca_apply, dry_run=bool(dry_run), override_orders=override_orders)

    if name in ("ibkr", "interactivebrokers", "interactive_brokers", "ib_gateway", "ibgateway", "tws"):
        if _ibkr_apply is None:
            return {"ok": False, "status": "ibkr_adapter_missing"}
        return _call_adapter(_ibkr_apply, dry_run=bool(dry_run), override_orders=override_orders)

    return {"ok": False, "status": "unknown_broker", "broker": name}


def apply_new_portfolio_orders_router(dry_run: bool = False, override_orders: Optional[List[dict]] = None) -> Dict[str, Any]:
    chain = _parse_failover_chain()
    if not chain:
        chain = ["sim"]

    attempts: List[Dict[str, Any]] = []

    for name in chain:
        t0 = time.time()
        try:
            res = _apply_one(name, dry_run=bool(dry_run), override_orders=override_orders) or {}
            ok = bool(res.get("ok", False))
            attempts.append(
                {
                    "broker": name,
                    "ok": ok,
                    "status": res.get("status"),
                    "dur_ms": int((time.time() - t0) * 1000),
                }
            )
            if ok:
                res.setdefault("broker", name)
                res["failover_attempts"] = attempts
                return res
        except Exception as e:
            attempts.append(
                {
                    "broker": name,
                    "ok": False,
                    "status": "exception",
                    "error": str(e),
                    "dur_ms": int((time.time() - t0) * 1000),
                }
            )
            continue

    return {"ok": False, "status": "all_brokers_failed", "failover_attempts": attempts}
