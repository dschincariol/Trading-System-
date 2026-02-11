"""
Applies latest portfolio_orders into broker_sim (paper execution)
and prints a JSON summary.

Execution safety:
- Kill switch enforced
- Execution mode enforced (paper / shadow / live)
- Live mode requires explicit arming
"""

import time
import json
import os
import logging
import sys
from typing import Any, Dict, List

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)

from dev_core.kill_switch import execution_allowed
from dev_core.rules_engine import evaluate_rules
from dev_core.execution_mode import get_execution_mode
from dev_core.broker_router import apply_new_portfolio_orders_router as apply_new_portfolio_orders
from execution_policy_engine import apply_execution_policy


# ============================================================
# Job / runtime config
# ============================================================

JOB_NAME = "broker_apply_orders"

OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)

PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "120"))
BROKER_NAME = os.environ.get("BROKER_NAME", "sim")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [broker_apply_orders] %(message)s",
)


# ============================================================
# Shadow intent logging
# ============================================================

def _log_shadow_intents(
    orders: List[Dict[str, Any]],
    actor: str,
    mode_state: dict,
) -> None:
    try:
        con = connect()
        try:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS shadow_order_intents (
                  ts_ms INTEGER NOT NULL,
                  actor TEXT NOT NULL,
                  broker TEXT NOT NULL,
                  orders_json TEXT NOT NULL,
                  mode_json TEXT NOT NULL
                )
                """
            )

            con.execute(
                """
                INSERT INTO shadow_order_intents(
                  ts_ms, actor, broker, orders_json, mode_json
                )
                VALUES (?,?,?,?,?)
                """,
                (
                    int(time.time() * 1000),
                    str(actor),
                    str(BROKER_NAME),
                    json.dumps(orders or [], separators=(",", ":"), sort_keys=True),
                    json.dumps(mode_state or {}, separators=(",", ":"), sort_keys=True),
                ),
            )

            con.commit()

        finally:
            con.close()

    except Exception:
        # fail-soft
        pass


def _print(out: Dict[str, Any]) -> None:
    print(json.dumps(out, indent=2, sort_keys=True))


# ============================================================
# Main
# ============================================================

def main() -> int:

    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        return 2

    started_ms = int(time.time() * 1000)

    try:

        # ============================================================
        # Kill switch (global hard safety)
        # ============================================================

        con = connect()
        try:
            allow, ks_reason, ks_meta = execution_allowed(
                con=con,
                symbol=None,
                regime=None,
            )
        finally:
            con.close()

        if not allow:
            _print(
                {
                    "status": "blocked",
                    "layer": "kill_switch",
                    "broker": BROKER_NAME,
                    "blocked_reason": ks_reason,
                    "blocked_meta": ks_meta,
                    "ts_ms": int(time.time() * 1000),
                    "dur_ms": int(time.time() * 1000) - started_ms,
                }
            )
            return 0

        # ============================================================
        # Rules engine (non-blocking)
        # ============================================================

        try:
            evaluate_rules()
        except Exception:
            pass

        # ============================================================
        # Execution mode
        # ============================================================

        mode_state = get_execution_mode() or {}
        mode = str(mode_state.get("mode") or "").lower().strip()

        # ============================================================
        # SHADOW MODE
        # ============================================================

        if mode == "shadow":

            preview = apply_new_portfolio_orders(dry_run=True) or {}
            raw_orders = list((preview or {}).get("orders") or [])
            shaped_orders = apply_execution_policy(raw_orders)

            res = {
                "orders": shaped_orders,
                "preview": preview,
            }

            _log_shadow_intents(
                shaped_orders,
                actor=OWNER,
                mode_state=mode_state,
            )

            _print(
                {
                    "status": "ok",
                    "mode": "shadow",
                    "broker": BROKER_NAME,
                    "executed": False,
                    "intents_logged": True,
                    "order_count": len(shaped_orders),
                    "preview": res,
                    "ts_ms": int(time.time() * 1000),
                    "dur_ms": int(time.time() * 1000) - started_ms,
                }
            )

            return 0

        # ============================================================
        # PAPER MODE
        # ============================================================

        if mode == "paper":

            broker_lc = str(BROKER_NAME).lower().strip()

            if broker_lc not in ("sim", "paper", "sandbox"):
                preview = apply_new_portfolio_orders(dry_run=True) or {}
                _print(
                    {
                        "status": "blocked",
                        "layer": "execution_mode",
                        "mode": "paper",
                        "broker": BROKER_NAME,
                        "reason": "paper_mode_requires_sim_broker",
                        "order_count": len((preview or {}).get("orders") or []),
                        "ts_ms": int(time.time() * 1000),
                        "dur_ms": int(time.time() * 1000) - started_ms,
                    }
                )
                return 0

            preview = apply_new_portfolio_orders(dry_run=True) or {}
            raw_orders = list((preview or {}).get("orders") or [])
            shaped_orders = apply_execution_policy(raw_orders)

            res = apply_new_portfolio_orders(
                dry_run=False,
                override_orders=shaped_orders,
            )

            _print(
                {
                    "status": "ok",
                    "mode": "paper",
                    "broker": BROKER_NAME,
                    "result": res,
                    "ts_ms": int(time.time() * 1000),
                    "dur_ms": int(time.time() * 1000) - started_ms,
                }
            )

            return 0

        # ============================================================
        # LIVE MODE
        # ============================================================

        if mode != "live":
            _print(
                {
                    "status": "blocked",
                    "layer": "execution_mode",
                    "mode": mode,
                    "broker": BROKER_NAME,
                    "reason": "mode_not_live",
                    "ts_ms": int(time.time() * 1000),
                    "dur_ms": int(time.time() * 1000) - started_ms,
                }
            )
            return 0

        armed = bool(mode_state.get("armed", False)) or (
            os.environ.get("EXECUTION_ARMED", "0") == "1"
        )

        if not armed:
            _print(
                {
                    "status": "blocked",
                    "layer": "execution_mode",
                    "mode": "live",
                    "broker": BROKER_NAME,
                    "reason": "live_mode_not_armed",
                    "ts_ms": int(time.time() * 1000),
                    "dur_ms": int(time.time() * 1000) - started_ms,
                }
            )
            return 0

        # ============================================================
        # LIVE execution
        # ============================================================

        preview = apply_new_portfolio_orders(dry_run=True) or {}
        raw_orders = list((preview or {}).get("orders") or [])
        shaped_orders = apply_execution_policy(raw_orders)

        res = apply_new_portfolio_orders(
            dry_run=False,
            override_orders=shaped_orders,
        )

        # ============================================================
        # Persist execution_meta
        # ============================================================

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
                    ("last_execution_source", "live_broker"),
                )

                con.execute(
                    """
                    INSERT INTO execution_meta(key, value)
                    VALUES(?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                    """,
                    (
                        "last_execution_broker",
                        str((res or {}).get("broker") or str(BROKER_NAME)),
                    ),
                )

                con.commit()

            finally:
                con.close()

        except Exception:
            pass

        _print(
            {
                "status": "ok",
                "mode": "live",
                "broker": BROKER_NAME,
                "result": res,
                "ts_ms": int(time.time() * 1000),
                "dur_ms": int(time.time() * 1000) - started_ms,
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
