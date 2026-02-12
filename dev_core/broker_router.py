# FILE: dev_core/broker_router.py
# REPLACE ENTIRE FILE WITH THIS (copy/paste)

"""
Unified Broker Router

Supports:
- sim
- alpaca
- ibkr

Failover:
  BROKER_FAILOVER="ibkr,alpaca"
  Retries on exception OR ok=False
  Returns structured failover_attempts

Supports:
- override_orders
- override_order_id
- override_ts_ms

Adds:
- Pre-Live Position Reconciliation Gate
  (blocks LIVE execution if broker positions mismatch baseline)
"""

import os
import time
from typing import Any, Dict, List, Optional


# ============================================================
# Adapter imports (best-effort; router remains loadable)
# ============================================================

try:
    from dev_core.broker_sim import apply_new_portfolio_orders as _sim_apply
except Exception:
    _sim_apply = None

try:
    from dev_core.broker_alpaca_rest import apply_latest_portfolio_orders_live as _alpaca_apply
except Exception:
    _alpaca_apply = None

try:
    from dev_core.broker_ibkr_gateway import apply_latest_portfolio_orders_live as _ibkr_apply
except Exception:
    _ibkr_apply = None

# Pre-live reconciliation gate (hard block on mismatch)
try:
    from dev_core.position_reconcile import pre_live_position_reconcile as _prelive_reconcile
except Exception:
    _prelive_reconcile = None


# ============================================================
# Helpers
# ============================================================

def _parse_failover_chain() -> List[str]:
    raw = (os.environ.get("BROKER_FAILOVER", "") or "").strip()
    if raw:
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if parts:
            return parts

    name = str(os.environ.get("BROKER_NAME", os.environ.get("BROKER", "sim")) or "sim").lower().strip()
    return [name] if name else ["sim"]


def _call_adapter(
    fn,
    *,
    dry_run: bool,
    override_orders: Optional[List[dict]],
    override_order_id: Optional[int],
    override_ts_ms: Optional[int],
):
    """
    Backward-compatible adapter wrapper.
    Allows older backends that may not accept new kwargs.
    """
    try:
        return fn(
            dry_run=bool(dry_run),
            override_orders=override_orders,
            override_order_id=override_order_id,
            override_ts_ms=override_ts_ms,
        )
    except TypeError:
        try:
            return fn(
                dry_run=bool(dry_run),
                override_orders=override_orders,
            )
        except TypeError:
            return fn(dry_run=bool(dry_run))


def _apply_one(
    name: str,
    *,
    dry_run: bool,
    override_orders: Optional[List[dict]] = None,
    override_order_id: Optional[int] = None,
    override_ts_ms: Optional[int] = None,
) -> Dict[str, Any]:

    name = (name or "").lower().strip()

    # ---------------- SIM (never reconciled) ----------------
    if name in ("sim", "paper", "sandbox"):
        if _sim_apply is None:
            return {"ok": False, "status": "sim_adapter_missing", "broker": name}
        res = _call_adapter(
            _sim_apply,
            dry_run=dry_run,
            override_orders=override_orders,
            override_order_id=override_order_id,
            override_ts_ms=override_ts_ms,
        ) or {}
        if isinstance(res, dict) and "broker" not in res:
            res["broker"] = "sim"
        return res

    # ---------------- LIVE BROKERS ----------------
    is_live = name in (
        "alpaca", "alpaca_rest",
        "ibkr", "interactivebrokers", "interactive_brokers",
        "ib_gateway", "ibgateway", "tws",
    )

    # Pre-live reconcile gate (never blocks dry_run)
    if is_live and (not bool(dry_run)) and (_prelive_reconcile is not None):
        gate = _prelive_reconcile(broker=name) or {}
        if not bool(gate.get("ok", False)):
            gate.setdefault("ok", False)
            gate.setdefault("status", "prelive_reconcile_block")
            gate["broker"] = name
            gate["fatal_reconcile"] = True
            return gate

    # ---------------- ALPACA ----------------
    if name in ("alpaca", "alpaca_rest"):
        if _alpaca_apply is None:
            return {"ok": False, "status": "alpaca_adapter_missing", "broker": name}
        res = _call_adapter(
            _alpaca_apply,
            dry_run=dry_run,
            override_orders=override_orders,
            override_order_id=override_order_id,
            override_ts_ms=override_ts_ms,
        ) or {}
        if isinstance(res, dict) and "broker" not in res:
            res["broker"] = "alpaca"
        return res

    # ---------------- IBKR ----------------
    if name in ("ibkr", "interactivebrokers", "interactive_brokers", "ib_gateway", "ibgateway", "tws"):
        if _ibkr_apply is None:
            return {"ok": False, "status": "ibkr_adapter_missing", "broker": name}
        res = _call_adapter(
            _ibkr_apply,
            dry_run=dry_run,
            override_orders=override_orders,
            override_order_id=override_order_id,
            override_ts_ms=override_ts_ms,
        ) or {}
        if isinstance(res, dict) and "broker" not in res:
            res["broker"] = "ibkr"
        return res

    return {"ok": False, "status": "unknown_broker", "broker": name}


# ============================================================
# Public Router
# ============================================================

def apply_new_portfolio_orders_router(
    dry_run: bool = False,
    override_orders: Optional[List[dict]] = None,
    override_order_id: Optional[int] = None,
    override_ts_ms: Optional[int] = None,
) -> Dict[str, Any]:

    chain = _parse_failover_chain()
    if not chain:
        chain = ["sim"]

    attempts: List[Dict[str, Any]] = []

    for name in chain:
        t0 = time.time()
        try:
            res = _apply_one(
                name,
                dry_run=bool(dry_run),
                override_orders=override_orders,
                override_order_id=override_order_id,
                override_ts_ms=override_ts_ms,
            ) or {}

            # If reconciliation gate trips → DO NOT failover
            if bool(res.get("fatal_reconcile", False)):
                attempts.append(
                    {
                        "broker": name,
                        "ok": False,
                        "status": res.get("status"),
                        "dur_ms": int((time.time() - t0) * 1000),
                    }
                )
                res.setdefault("broker", name)
                res["failover_attempts"] = attempts
                return res

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
