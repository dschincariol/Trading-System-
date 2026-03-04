# portfolio_rebalance.py
"""
Runs portfolio rebalance (paper-trading intent only).

Outputs:
- summary JSON to stdout (captured in dashboard console)
"""

import time
import json
import os
import logging
from typing import Tuple, List, Dict, Any

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)

from engine.portfolio import compute_rebalance, get_portfolio_snapshot

try:
    from engine.portfolio import build_portfolio_intents, place_order
except Exception as e:
    raise RuntimeError(
        "portfolio_rebalance requires build_portfolio_intents() and place_order() "
        "to exist in dev_core.portfolio"
    ) from e

from engine.kill_switch import execution_allowed, activate
from engine.model_v2 import get_current_regime
from engine.rules_engine import evaluate_rules
from engine.regime_size import regime_capital_scale
from engine.opportunity_allocation import opportunity_weight

# ------            -- ------------------------------------------------------
# Optional health gate (fail-closed if present)
# ------            -- ------------------------------------------------------
try:
    from engine.health import get_health_snapshot
except Exception:
    def get_health_snapshot() -> Dict[str, Any]:
        return {"ok": True}

# ------            -- ------------------------------------------------------
# Job / runtime config
# ------            -- ------------------------------------------------------

JOB_NAME = "portfolio_rebalance"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

REBALANCE_INTERVAL_S = int(os.environ.get("REBALANCE_INTERVAL_S", "60"))
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

# Hard risk circuit breakers (fail-closed)
MAX_DRAWDOWN_PCT = float(os.environ.get("MAX_DRAWDOWN_PCT", "0.15"))      # 15%
MAX_DAILY_LOSS_PCT = float(os.environ.get("MAX_DAILY_LOSS_PCT", "0.05"))  # 5%
MIN_CONFIDENCE = float(os.environ.get("MIN_EXEC_CONFIDENCE", "0.25"))

# Opportunity-weighted allocation knobs (bounded, convex)
OPP_CONVEX_POWER = float(os.environ.get("OPP_CONVEX_POWER", "2.0"))
OPP_MIN_CAP = float(os.environ.get("OPP_MIN_CAP", "0.0"))
OPP_MAX_CAP = float(os.environ.get("OPP_MAX_CAP", "1.0"))
MAX_SINGLE_POSITION_WEIGHT = float(os.environ.get("MAX_SINGLE_POSITION_WEIGHT", "0.15"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [portfolio_rebalance] %(message)s",
)

# ------            -- ------------------------------------------------------
# Risk state (fail-closed)
# ------            -- ------------------------------------------------------

def _risk_state(con) -> Tuple[bool, str]:
    """
    Returns (ok: bool, reason: str)
    """
    try:
        # Max drawdown
        row = con.execute(
            "SELECT MAX(equity) - MIN(equity) FROM portfolio_bt_points"
        ).fetchone()
        dd_abs = float(row[0] or 0.0)

        row2 = con.execute(
            "SELECT MAX(equity) FROM portfolio_bt_points"
        ).fetchone()
        peak = float(row2[0] or 0.0)

        drawdown_pct = (dd_abs / peak) if peak > 0 else 0.0
        if drawdown_pct >= MAX_DRAWDOWN_PCT:
            return False, f"max_drawdown_exceeded pct={drawdown_pct:.3f}"

        # Daily loss
        row3 = con.execute(
            """
            SELECT SUM(ret)
            FROM portfolio_bt_points
            WHERE ts_ms >= ?
            """,
            (int((time.time() - 86400) * 1000),),
        ).fetchone()
        daily_ret = float(row3[0] or 0.0)

        if daily_ret <= -MAX_DAILY_LOSS_PCT:
            return False, f"max_daily_loss_exceeded ret={daily_ret:.3f}"

        return True, "ok"

    except Exception as e:
        return False, f"risk_state_error {e}"

# ------            -- ------------------------------------------------------
# Main loop
# ------            -- ------------------------------------------------------

def main() -> int:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        return 2

    last_hb_s = 0.0

    try:
        while True:
            con = connect()
            try:
                # ---            -- ------------------------------------------------------
                # Phase 5/6/7: rules engine (global + per-symbol halts)
                # ---            -- ------------------------------------------------------
                try:
                    evaluate_rules()
                except Exception:
                    pass

                # Current regime (best-effort)
                regime = None

                try:
                    regime = str(get_current_regime() or "").strip()
                except Exception:
                    regime = None

                # Kill switch gate (fail-closed)
                allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=regime)
                if not allow:
                    logging.error("EXECUTION_HALTED %s meta=%s", ks_reason, ks_meta)
                    return 5

                # Health gate (auto-kill global on failure)
                health = get_health_snapshot()
                if not health.get("ok", False):
                    try:
                        activate(
                            "global",
                            "global",
                            reason="auto_health_failure",
                            actor="system",
                            meta={"health": health, "job": JOB_NAME},
                            action="AUTO",
                            con=con,
                        )
                    except Exception:
                        pass
                    logging.error("EXECUTION_HALTED health_degraded=%s", health)
                    return 3

                # Risk gate (auto-kill global on failure)
                ok, reason = _risk_state(con)
                if not ok:
                    try:
                        activate(
                            "global",
                            "global",
                            reason=f"auto_risk_gate:{reason}",
                            actor="system",
                            meta={"job": JOB_NAME},
                            action="AUTO",
                            con=con,
                        )
                    except Exception:
                        pass
                    logging.error("EXECUTION_HALTED %s", reason)
                    return 4

                # Compute rebalance (pure compute)
                rebalance_res = compute_rebalance()

                # Regime capital scaling (best-effort, deterministic)
                try:
                    regime_info = regime_capital_scale(con)
                except Exception:
                    regime_info = {"ok": False}
                try:
                    regime_mult = float(regime_info.get("final_mult", 1.0))
                except Exception:
                    regime_mult = 1.0

                # Build intents
                intents: List[Dict[str, Any]] = build_portfolio_intents(con)
                executed: List[Dict[str, Any]] = []
                skipped: List[Dict[str, Any]] = []

                # ------            -- ------------------------------------------------------
                # Filter + execute intents (paper/intent-only)
                # ------            -- ------------------------------------------------------
                for it in intents:
                    conf = float(it.get("confidence", 0.0))
                    if conf < MIN_CONFIDENCE:
                        logging.info(
                            "SKIP_LOW_CONF symbol=%s conf=%.2f",
                            it.get("symbol"),
                            conf,
                        )
                        skipped.append(it)
                        continue

                    # Execution confidence (best-effort; default 1.0)
                    try:
                        exec_conf = float(it.get("execution_confidence", 1.0))
                    except Exception:
                        exec_conf = 1.0

                    # Opportunity-weighted multiplier (bounded, convex)
                    try:
                        opp_mult = opportunity_weight(
                            signal_conf=conf,
                            regime_mult=regime_mult,
                            exec_conf=exec_conf,
                            max_cap=float(OPP_MAX_CAP),
                            min_cap=float(OPP_MIN_CAP),
                            convex_power=float(OPP_CONVEX_POWER),
                        )
                    except Exception:
                        opp_mult = 0.0

                    if float(opp_mult) <= 0.0:
                        logging.info(
                            "SKIP_NO_OPPORTUNITY symbol=%s conf=%.2f exec_conf=%.2f regime_mult=%.2f",
                            it.get("symbol"),
                            conf,
                            exec_conf,
                            regime_mult,
                        )
                        it2 = dict(it)
                        it2["blocked_reason"] = "opportunity_weight_zero"
                        it2["blocked_meta"] = {
                            "confidence": conf,
                            "execution_confidence": exec_conf,
                            "regime_mult": regime_mult,
                            "opp_mult": float(opp_mult),
                        }
                        skipped.append(it2)
                        continue

                    # Apply multiplier to target_weight (if present)
                    try:
                        tw = float(it.get("target_weight", 0.0))
                        tw2 = float(tw) * float(opp_mult)

                        # Hard single-position cap (abs)
                        cap = float(MAX_SINGLE_POSITION_WEIGHT)
                        if cap > 0:
                            if tw2 > cap:
                                tw2 = cap
                            elif tw2 < -cap:
                                tw2 = -cap

                        it["target_weight"] = float(tw2)
                        it["opp_mult"] = float(opp_mult)
                        it["regime_mult"] = float(regime_mult)
                    except Exception:
                        # if target_weight isn't a thing in this intent schema, still allow through
                        it["opp_mult"] = float(opp_mult)
                        it["regime_mult"] = float(regime_mult)

                    sym = str(it.get("symbol") or "").strip()
                    # Kill switch per-symbol/per-regime (fail-closed)
                    allow2, ks_reason2, ks_meta2 = execution_allowed(con=con, symbol=sym, regime=regime)
                    if not allow2:
                        it2 = dict(it)
                        it2["blocked_reason"] = ks_reason2
                        it2["blocked_meta"] = ks_meta2
                        skipped.append(it2)
                        logging.error("SKIP_KILL_SWITCH symbol=%s %s meta=%s", sym, ks_reason2, ks_meta2)
                        continue

                    # Paper / intent-only execution
                    try:
                        place_order(it)
                        executed.append(it)
                    except Exception as e:
                        it2 = dict(it)
                        it2["blocked_reason"] = "place_order_error"
                        it2["blocked_meta"] = {"error": str(e)}
                        skipped.append(it2)

                # Snapshot portfolio state
                snapshot = get_portfolio_snapshot(limit_orders=30)

                out = {
                    "status": "ok",
                    "rebalance": rebalance_res,
                    "regime": regime_info,
                    "executed": executed,
                    "skipped": skipped,
                    "portfolio": snapshot,
                    "ts_ms": int(time.time() * 1000),
                }
                print(json.dumps(out, indent=2))

            finally:
                con.close()

            # ------            -- ------------------------------------------------------
            # Heartbeat + sleep
            # ------            -- ------------------------------------------------------
            now_s = time.time()
            if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
                try:
                    touch_job_lock(JOB_NAME, OWNER, PID)
                    put_job_heartbeat(
                        JOB_NAME,
                        OWNER,
                        PID,
                        extra_json=json.dumps(
                            {"interval_s": REBALANCE_INTERVAL_S},
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    )
                except Exception:
                    pass
                last_hb_s = now_s

            time.sleep(REBALANCE_INTERVAL_S)

    except Exception:
        logging.exception("portfolio rebalance failed")
        raise

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass

# ------            -- ------------------------------------------------------
# Ensure lock release on shutdown
# ------            -- ------------------------------------------------------

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass
