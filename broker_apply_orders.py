# broker_apply_orders.py
"""
Unified broker_apply_orders

Preserves:
- Job lock enforcement
- Kill switch
- Execution mode enforcement
- Execution Policy Engine shaping (EPE)
- Dual IBKR execution (optional)
- Shadow logging
- Execution meta tracking

Adds:
- Reads latest row-per-order portfolio_orders batch (no orders_json dependency) when available
- Hard TTL enforcement via EPE (fail-closed, when supported by EPE)
- ALE registration on-demand (via EPE, when supported by EPE)
"""

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from dev_core.storage import connect, init_db, acquire_job_lock, release_job_lock
from dev_core.kill_switch import execution_allowed
from dev_core.position_reconcile import pre_live_position_reconcile
from dev_core.adaptive_order_slicer import AdaptiveOrderSlicer
from dev_core.portfolio_risk_gate import apply_execution_risk_governor
from dev_core.rules_engine import evaluate_rules
from dev_core.execution_mode import get_execution_mode
from dev_core.regime_stack import compute_regime_vector, regime_compatibility, regime_model_version
from dev_core.broker_router import apply_new_portfolio_orders_router as apply_new_portfolio_orders

# Newer path (preferred)
try:
    from dev_core.portfolio_execution_intents import load_latest_execution_intents  # type: ignore
except Exception:
    load_latest_execution_intents = None  # type: ignore

# EPE import (support both module names)
try:
    from dev_core.execution_policy_engine import apply_execution_policy  # type: ignore
except Exception:
    try:
        from execution_policy_engine import apply_execution_policy  # type: ignore
    except Exception as e:
        raise RuntimeError(f"apply_execution_policy import failed: {e}")

# Optional dual execution (IBKR)
try:
    from dev_core.dual_execution import apply_latest_portfolio_orders_dual_ibkr  # type: ignore
except Exception:
    apply_latest_portfolio_orders_dual_ibkr = None  # type: ignore


JOB_NAME = "broker_apply_orders"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "120"))
BROKER_NAME = os.environ.get("BROKER_NAME", "sim")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper().strip()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [broker_apply_orders] %(message)s",
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _print(out: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(out, sort_keys=True) + "\n")
    sys.stdout.flush()


def _ensure_shadow_table(con) -> None:
    # Create a superset schema to maximize compatibility.
    # NOTE: CREATE TABLE IF NOT EXISTS does not alter existing tables.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS shadow_order_intents (
          ts_ms INTEGER NOT NULL,
          actor TEXT NOT NULL,
          broker TEXT NOT NULL,
          orders_json TEXT,
          intents_json TEXT,
          mode_json TEXT NOT NULL
        )
        """
    )


def _log_shadow_intents(payload: List[Dict[str, Any]], actor: str, mode_state: dict) -> None:
    # Backward compatible insert: try intents_json, then orders_json, then minimal.
    try:
        con = connect()
        try:
            _ensure_shadow_table(con)

            payload_json = json.dumps(payload or [], separators=(",", ":"), sort_keys=True)
            mode_json = json.dumps(mode_state or {}, separators=(",", ":"), sort_keys=True)

            try:
                con.execute(
                    """
                    INSERT INTO shadow_order_intents(
                      ts_ms, actor, broker, intents_json, mode_json
                    )
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        _now_ms(),
                        str(actor),
                        str(BROKER_NAME),
                        payload_json,
                        mode_json,
                    ),
                )
            except Exception:
                try:
                    con.execute(
                        """
                        INSERT INTO shadow_order_intents(
                          ts_ms, actor, broker, orders_json, mode_json
                        )
                        VALUES (?,?,?,?,?)
                        """,
                        (
                            _now_ms(),
                            str(actor),
                            str(BROKER_NAME),
                            payload_json,
                            mode_json,
                        ),
                    )
                except Exception:
                    con.execute(
                        """
                        INSERT INTO shadow_order_intents(
                          ts_ms, actor, broker, mode_json
                        )
                        VALUES (?,?,?,?)
                        """,
                        (
                            _now_ms(),
                            str(actor),
                            str(BROKER_NAME),
                            mode_json,
                        ),
                    )

            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _write_execution_meta_last(broker: str, source: str) -> None:
    try:
        con = connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                INSERT INTO execution_meta(key, value)
                VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("last_execution_source", str(source)),
            )
            con.execute(
                """
                INSERT INTO execution_meta(key, value)
                VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                ("last_execution_broker", str(broker)),
            )
            con.commit()
        finally:
            con.close()
    except Exception:
        pass


def _extract_preview_meta(preview: dict) -> Tuple[Optional[int], Optional[int], List[dict]]:
    oid = None
    ts_ms = None
    orders: List[dict] = []

    if isinstance(preview, dict):
        for k in ("order_id", "portfolio_orders_id", "id"):
            try:
                if preview.get(k) is not None:
                    oid = int(preview.get(k))
                    break
            except Exception:
                pass

        for k in ("ts_ms", "portfolio_orders_ts_ms"):
            try:
                if preview.get(k) is not None:
                    ts_ms = int(preview.get(k))
                    break
            except Exception:
                pass

        try:
            orders = list(preview.get("orders") or [])
        except Exception:
            orders = []

    return oid, ts_ms, orders


def _acquire_lock_compat() -> bool:
    # Support both signatures:
    # - acquire_job_lock(name, owner, pid, stale_after_s=...)
    # - acquire_job_lock(name, owner, pid, ttl_s=...)
    try:
        return bool(acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S))
    except TypeError:
        return bool(acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S))


def _apply_epe_compat(
    *,
    con,
    raw_payload: List[dict],
    actor: str,
    mode: str,
    broker: str,
    portfolio_orders_id: Optional[int],
    portfolio_orders_batch_id: Optional[int],
    default_signal_ts_ms: Optional[int],
) -> List[dict]:
    # Support both EPE signatures:
    # Newer:
    #   apply_execution_policy(con=..., intents=..., actor=..., mode=..., broker=...,
    #                          portfolio_orders_batch_id=..., default_signal_ts_ms=...)
    # Older:
    #   apply_execution_policy(orders, actor=..., mode=..., broker=..., portfolio_orders_id=...,
    #                          default_signal_ts_ms=...)
    try:
        shaped = apply_execution_policy(
            con=con,
            intents=raw_payload,
            actor=str(actor),
            mode=str(mode),
            broker=str(broker),
            portfolio_orders_batch_id=(int(portfolio_orders_batch_id) if portfolio_orders_batch_id is not None else None),
            portfolio_orders_id=(int(portfolio_orders_id) if portfolio_orders_id is not None else None),
            default_signal_ts_ms=(int(default_signal_ts_ms) if default_signal_ts_ms is not None else None),
        )
        return list(shaped or [])
    except TypeError:
        shaped = apply_execution_policy(
            raw_payload,
            actor=str(actor),
            mode=str(mode),
            broker=str(broker),
            portfolio_orders_id=(int(portfolio_orders_id) if portfolio_orders_id is not None else None),
            default_signal_ts_ms=(int(default_signal_ts_ms) if default_signal_ts_ms is not None else None),
        )
        return list(shaped or [])


def _load_latest_payload() -> Tuple[Optional[int], Optional[int], List[dict], str]:
    """
    Returns: (batch_or_order_id, ts_ms, payload_list, source)
    payload_list is either intents or orders; broker router receives as override_orders.
    """
    # Preferred: row-per-order intents table
    if callable(load_latest_execution_intents):
        try:
            con = connect()
            try:
                batch = load_latest_execution_intents(con) or {}
                batch_id = batch.get("batch_id")
                batch_ts_ms = batch.get("batch_ts_ms")
                intents = list(batch.get("intents") or [])
            finally:
                con.close()

            if (batch_id is not None) or intents:
                return (
                    (int(batch_id) if batch_id is not None else None),
                    (int(batch_ts_ms) if batch_ts_ms is not None else None),
                    intents,
                    "execution_intents",
                )
        except Exception:
            pass

    # Fallback: legacy broker_router dry_run preview
    preview = apply_new_portfolio_orders(dry_run=True)
    oid, ts_ms, orders = _extract_preview_meta(preview)
    return oid, ts_ms, orders, "broker_router_preview"


def main() -> int:
    init_db()

    if not _acquire_lock_compat():
        _print({"status": "locked_out", "job": JOB_NAME})
        return 0

    started_ms = _now_ms()

    try:
        con = connect()
        try:
            allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=None)
        finally:
            con.close()

        if not allow:
            _print(
                {
                    "status": "blocked",
                    "layer": "kill_switch",
                    "reason": ks_reason,
                    "meta": ks_meta,
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        # Best-effort rules eval (never blocks)
        try:
            evaluate_rules()
        except Exception:
            pass

        mode_state = get_execution_mode() or {}
        mode = str(mode_state.get("mode") or "").lower().strip()

        # Load latest payload
        batch_or_oid, payload_ts_ms, raw_payload, payload_source = _load_latest_payload()

        # Shape via EPE (TTL hard wall / ALE registration when supported)
        con = connect()
        try:
            shaped_payload = _apply_epe_compat(
                con=con,
                raw_payload=raw_payload,
                actor=str(OWNER),
                mode=str(mode),
                broker=str(BROKER_NAME),
                portfolio_orders_id=(int(batch_or_oid) if batch_or_oid is not None else None),
                portfolio_orders_batch_id=(int(batch_or_oid) if batch_or_oid is not None else None),
                default_signal_ts_ms=(int(payload_ts_ms) if payload_ts_ms is not None else None),
            )
            try:
                con.commit()
            except Exception:
                pass
        finally:
            con.close()

        if mode == "shadow":
            _log_shadow_intents(shaped_payload, OWNER, mode_state)
            _print(
                {
                    "status": "ok",
                    "mode": "shadow",
                    "broker": BROKER_NAME,
                    "payload_source": payload_source,
                    "batch_id": batch_or_oid,
                    "raw_count": len(raw_payload),
                    "shaped_count": len(shaped_payload),
                    "executed": False,
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        if mode == "paper":
            res = apply_new_portfolio_orders(
                dry_run=False,
                override_orders=shaped_payload,
                override_order_id=(int(batch_or_oid) if batch_or_oid is not None else None),
                override_ts_ms=(int(payload_ts_ms) if payload_ts_ms is not None else None),
            )
            _write_execution_meta_last(BROKER_NAME, "paper_broker_sim")
            _print(
                {
                    "status": "ok",
                    "mode": "paper",
                    "broker": BROKER_NAME,
                    "payload_source": payload_source,
                    "batch_id": batch_or_oid,
                    "result": res,
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        if mode != "live":
            _print(
                {
                    "status": "blocked",
                    "layer": "execution_mode",
                    "mode": mode,
                    "broker": BROKER_NAME,
                    "reason": "mode_not_live",
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        armed = (int(mode_state.get("armed", 0)) == 1) or (os.environ.get("EXECUTION_ARMED", "0") == "1")
        if not armed:
            _print(
                {
                    "status": "blocked",
                    "layer": "execution_mode",
                    "mode": "live",
                    "broker": BROKER_NAME,
                    "reason": "live_not_armed",
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        # ------------------------------------------------------------
        # Institutional completion layer (pre-trade):
        # 1) Position reconciliation (live brokers)
        # 2) Execution risk governor (defense in depth)
        # ------------------------------------------------------------
        try:
            con2 = connect()
            try:
                rec = pre_live_position_reconcile(
                    con2,
                    broker=str(BROKER_NAME or ""),
                    fatal_net_liq_mismatch_pct=float(os.environ.get("EXEC_FATAL_NET_LIQ_MISMATCH_PCT", "0.02")),
                    fatal_abs_pos_usd=float(os.environ.get("EXEC_FATAL_ABS_POS_USD", "250.0")),
                )
                if isinstance(rec, dict) and rec.get("fatal_reconcile"):
                    _print(
                        {
                            "status": "blocked",
                            "layer": "position_reconcile",
                            "mode": "live",
                            "broker": BROKER_NAME,
                            "reconcile": rec,
                            "ts_ms": _now_ms(),
                            "dur_ms": _now_ms() - started_ms,
                        }
                    )
                    return 0
            finally:
                con2.close()
        except Exception as e:
            _print(
                {
                    "status": "blocked",
                    "layer": "position_reconcile_exception",
                    "mode": "live",
                    "broker": BROKER_NAME,
                    "error": str(e),
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        try:
            con3 = connect()
            try:
                governed, gov_info = apply_execution_risk_governor(
                    con3,
                    list(shaped_payload or []),
                    broker=str(BROKER_NAME or ""),
                    mode="live",
                    equity_usd=None,
                )
            finally:
                con3.close()
        except Exception as e:
            _print(
                {
                    "status": "blocked",
                    "layer": "risk_governor_exception",
                    "mode": "live",
                    "broker": BROKER_NAME,
                    "error": str(e),
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        if isinstance(gov_info, dict) and (not gov_info.get("ok")):
            _print(
                {
                    "status": "blocked",
                    "layer": "risk_governor",
                    "mode": "live",
                    "broker": BROKER_NAME,
                    "governor": gov_info,
                    "ts_ms": _now_ms(),
                    "dur_ms": _now_ms() - started_ms,
                }
            )
            return 0

        shaped_payload = list(governed or [])

        dual_enable = os.environ.get("EXECUTION_DUAL_ENABLE", "0") == "1"

        if dual_enable and str(BROKER_NAME).lower() == "ibkr" and callable(apply_latest_portfolio_orders_dual_ibkr):
            res = apply_latest_portfolio_orders_dual_ibkr(dry_run_live=False)
        else:
            res = apply_new_portfolio_orders(
                dry_run=False,
                override_orders=shaped_payload,
                override_order_id=(int(batch_or_oid) if batch_or_oid is not None else None),
                override_ts_ms=(int(payload_ts_ms) if payload_ts_ms is not None else None),
            )

        broker_used = str((res or {}).get("broker") or BROKER_NAME)
        _write_execution_meta_last(broker_used, "live_broker")

        # ------------------------------------------------------------
        # Institutional completion layer (post-trade):
        # ------------------------------------------------------------
        try:
            from dev_core.execution_analytics_engine import build_execution_analytics
            build_execution_analytics(limit=2000)
        except Exception:
            pass

        _print(
            {
                "status": "ok",
                "mode": "live",
                "broker": BROKER_NAME,
                "broker_used": broker_used,
                "payload_source": payload_source,
                "batch_id": batch_or_oid,
                "result": res,
                "ts_ms": _now_ms(),
                "dur_ms": _now_ms() - started_ms,
            }
        )
        return 0
    except Exception as e:
        sys.stderr.write(f"[broker_apply_orders] ERROR: {e}\n")
        return 2
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
