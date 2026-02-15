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
import json
from typing import Any, Dict, List, Optional

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
    from engine.dev_core.kill_switch import snapshot as _kill_switch_snapshot  # type: ignore
except Exception:
    _kill_switch_snapshot = None  # type: ignore

try:
    from engine.dev_core.execution_mode import get_execution_mode as _get_execution_mode  # type: ignore
except Exception:
    _get_execution_mode = None  # type: ignore

# Best-effort DB access for realized slippage distribution
try:
    from engine.dev_core.storage import connect  # type: ignore
except Exception:
    connect = None  # type: ignore

# Adaptive slicing (best-effort; router remains loadable)
try:
    from engine.dev_core.adaptive_order_slicer import AdaptiveOrderSlicer  # type: ignore
except Exception:
    AdaptiveOrderSlicer = None  # type: ignore


# ============================================================
# Adapter imports (best-effort; router remains loadable)
# ============================================================

try:
    from engine.dev_core.broker_sim import apply_new_portfolio_orders as _sim_apply
except Exception:
    _sim_apply = None

try:
    from engine.dev_core.broker_alpaca_rest import apply_latest_portfolio_orders_live as _alpaca_apply
except Exception:
    _alpaca_apply = None

try:
    from engine.dev_core.broker_ibkr_gateway import apply_latest_portfolio_orders_live as _ibkr_apply
except Exception:
    _ibkr_apply = None

# Pre-live reconciliation gate (hard block on mismatch)
try:
    from engine.dev_core.position_reconcile import pre_live_position_reconcile as _prelive_reconcile
except Exception:
    _prelive_reconcile = None


# ============================================================
# Helpers
# ============================================================

def _jobs_from_db_snapshot() -> List[Dict[str, Any]]:
    """
    Minimal job list for compute_system_state() when routing execution outside JobManager.
    Uses job_locks.heartbeat_ts_ms to infer running-ness.
    """
    if connect is None:
        return []

    try:
        max_stale_s = float(os.environ.get("HEALTH_JOBS_MAX_STALE_S", "180"))
    except Exception:
        max_stale_s = 180.0

    now_ms = int(time.time() * 1000)

    try:
        con = connect(readonly=True)
        try:
            rows = con.execute(
                "SELECT job_name, heartbeat_ts_ms FROM job_locks"
            ).fetchall()
        finally:
            try:
                con.close()
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


def _execution_gate_or_block(dry_run: bool) -> Optional[Dict[str, Any]]:
    """
    Returns None if allowed, else a structured block response.
    Fail-closed if providers missing.
    """
    if bool(dry_run):
        return None

    if _execution_gate_snapshot is None:
        return {"ok": False, "status": "execution_blocked_gate_unavailable"}

    if _kill_switch_snapshot is None or _get_execution_mode is None:
        return {"ok": False, "status": "execution_blocked_gate_providers_missing"}

    gate = _execution_gate_snapshot(
        get_jobs=_jobs_from_db_snapshot,
        get_kill_switches=lambda: (_kill_switch_snapshot() or {}),
        get_execution_mode=lambda: (_get_execution_mode() or {}),
    )

    if not bool(gate.get("ok")):
        return {"ok": False, "status": "execution_blocked", "gate": gate}

    return None


def _load_recent_slippage_bps(symbol: str, broker: str, n: int = 80) -> List[float]:
    """
    Loads recent realized slippage_bps for (symbol, broker) from execution_analytics.
    Falls back to empty on any error.
    """
    if connect is None:
        return []

    sym = str(symbol or "").upper().strip()
    br = str(broker or "").lower().strip()
    n = int(max(10, min(500, int(n))))

    try:
        con = connect()
        try:
            rows = con.execute(
                """
                SELECT slippage_bps
                FROM execution_analytics
                WHERE symbol = ?
                  AND (broker = ? OR broker IS NULL)
                  AND slippage_bps IS NOT NULL
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (sym, br, n),
            ).fetchall()
            out = []
            for (v,) in rows or []:
                try:
                    out.append(float(v))
                except Exception:
                    pass
            return out
        finally:
            con.close()
    except Exception:
        return []

def _parse_failover_chain() -> List[str]:
    raw = (os.environ.get("BROKER_FAILOVER", "") or "").strip()
    if raw:
        parts = [p.strip().lower() for p in raw.split(",") if p.strip()]
        if parts:
            return parts

    name = str(os.environ.get("BROKER_NAME", os.environ.get("BROKER", "sim")) or "sim").lower().strip()
    return [name] if name else ["sim"]


def _adaptive_execute_orders(
    *,
    broker_name: str,
    fn,
    dry_run: bool,
    override_orders: List[dict],
    override_order_id: Optional[int],
    override_ts_ms: Optional[int],
) -> Dict[str, Any]:
    """
    Executes override_orders using AdaptiveOrderSlicer:
    - Splits each order into slices
    - Sleeps between slices (delay_ms)
    - Uses recent realized slippage VARIANCE / tails (bps) from execution_analytics
    - Attaches audit fields into order dict (extra_json persistence happens downstream)
    """
    if not override_orders:
        return {"ok": True, "status": "no_orders", "broker": broker_name}

    # Feature flag
    if os.environ.get("EXEC_ADAPTIVE_SLICING", "1") != "1":
        # caller will run normal batch
        return {"ok": False, "status": "adaptive_disabled"}  # sentinel

    if AdaptiveOrderSlicer is None:
        return {"ok": False, "status": "adaptive_slicer_missing"}  # hard fail; keeps you honest

    slice_audit: List[Dict[str, Any]] = []
    all_ok = True
    last_res: Dict[str, Any] = {}

    for oidx, order in enumerate(list(override_orders or [])):
        try:
            symbol = str(order.get("symbol") or "").upper().strip()
            if not symbol:
                continue

            qty0 = float(order.get("qty") or 0.0)
            if qty0 == 0.0:
                continue

            side_sign = 1.0 if qty0 > 0 else -1.0
            remaining = abs(qty0)

            # Caller may provide spread/vol; if not, default 0 (slicer still uses realized distribution)
            spread_bps = float(order.get("spread_bps") or order.get("spread") or 0.0)
            vol_bps = float(order.get("vol_bps") or order.get("volatility") or 0.0)

            recent_slip = _load_recent_slippage_bps(symbol, broker_name, n=80)

            slice_index = 0
            while remaining > 0:
                slicer = AdaptiveOrderSlicer(
                    recent_slippage_bps=recent_slip,
                    spread_bps=spread_bps,
                    volatility_bps=vol_bps,
                    symbol=symbol,
                    broker=broker_name,
                )
                plan = slicer.compute_slice_plan(remaining_qty=remaining)

                if bool(plan.get("abort")):
                    slice_audit.append(
                        {
                            "symbol": symbol,
                            "order_index": int(oidx),
                            "slice_index": int(slice_index),
                            "abort": True,
                            "abort_reason": plan.get("abort_reason"),
                            "audit": plan.get("audit") or {},
                        }
                    )
                    all_ok = False
                    break

                slice_qty = float(plan.get("slice_qty") or 0.0)
                if slice_qty <= 0.0:
                    all_ok = False
                    break

                # Build slice order (preserve original fields)
                so = dict(order)
                so["qty"] = float(slice_qty) * float(side_sign)

                # Audit fields (persist downstream via extra_json)
                audit = plan.get("audit") or {}
                so["adaptive_slice"] = True
                so["adaptive_slice_plan"] = audit
                so["adaptive_slice_index"] = int(slice_index)
                so["adaptive_parent_qty"] = float(qty0)

                # Execute single slice as a 1-order batch
                r = _call_adapter(
                    fn,
                    dry_run=bool(dry_run),
                    override_orders=[so],
                    override_order_id=override_order_id,
                    override_ts_ms=override_ts_ms,
                ) or {}

                last_res = dict(r) if isinstance(r, dict) else {"raw": r}
                ok = bool(last_res.get("ok", False))

                slice_audit.append(
                    {
                        "symbol": symbol,
                        "order_index": int(oidx),
                        "slice_index": int(slice_index),
                        "slice_qty": float(slice_qty) * float(side_sign),
                        "delay_ms": int(plan.get("delay_ms") or 0),
                        "slow_down": bool(plan.get("slow_down")),
                        "ok": ok,
                        "status": last_res.get("status"),
                        "audit": audit,
                    }
                )

                if not ok:
                    all_ok = False
                    break

                remaining -= float(slice_qty)
                slice_index += 1

                delay_ms = int(plan.get("delay_ms") or 0)
                if delay_ms > 0 and remaining > 0 and (not bool(dry_run)):
                    time.sleep(float(delay_ms) / 1000.0)

        except Exception as e:
            all_ok = False
            slice_audit.append({"order_index": int(oidx), "exception": str(e)})

    out: Dict[str, Any] = {}
    out.update(last_res if isinstance(last_res, dict) else {})
    out["ok"] = bool(all_ok)
    out.setdefault("status", "adaptive_sliced" if all_ok else "adaptive_failed")
    out["adaptive_slices"] = slice_audit
    return out


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
        if (not bool(dry_run)) and override_orders:
            ad = _adaptive_execute_orders(
                broker_name="sim",
                fn=_sim_apply,
                dry_run=dry_run,
                override_orders=list(override_orders or []),
                override_order_id=override_order_id,
                override_ts_ms=override_ts_ms,
            )
            if isinstance(ad, dict) and ad.get("status") != "adaptive_disabled":
                res = ad or {}
            else:
                res = _call_adapter(
                    _sim_apply,
                    dry_run=dry_run,
                    override_orders=override_orders,
                    override_order_id=override_order_id,
                    override_ts_ms=override_ts_ms,
                ) or {}
        else:
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
        if (not bool(dry_run)) and override_orders:
            ad = _adaptive_execute_orders(
                broker_name="alpaca",
                fn=_alpaca_apply,
                dry_run=dry_run,
                override_orders=list(override_orders or []),
                override_order_id=override_order_id,
                override_ts_ms=override_ts_ms,
            )
            if isinstance(ad, dict) and ad.get("status") != "adaptive_disabled":
                res = ad or {}
            else:
                res = _call_adapter(
                    _alpaca_apply,
                    dry_run=dry_run,
                    override_orders=override_orders,
                    override_order_id=override_order_id,
                    override_ts_ms=override_ts_ms,
                ) or {}
        else:
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
        if (not bool(dry_run)) and override_orders:
            ad = _adaptive_execute_orders(
                broker_name="ibkr",
                fn=_ibkr_apply,
                dry_run=dry_run,
                override_orders=list(override_orders or []),
                override_order_id=override_order_id,
                override_ts_ms=override_ts_ms,
            )
            if isinstance(ad, dict) and ad.get("status") != "adaptive_disabled":
                res = ad or {}
            else:
                res = _call_adapter(
                    _ibkr_apply,
                    dry_run=dry_run,
                    override_orders=override_orders,
                    override_order_id=override_order_id,
                    override_ts_ms=override_ts_ms,
                ) or {}
        else:
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

    blocked = _execution_gate_or_block(dry_run=bool(dry_run))
    if blocked is not None:
        return blocked

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

