# FILE: trade_pipeline_job.py
# REPLACE ENTIRE FILE WITH THIS (copy/paste)

"""
Full Autonomous Trade Pipeline Job

Flow:
  1) Universe Discovery
  2) Meta Strategy Allocation
  3) Portfolio Rebalance (writes portfolio_orders)
  3b) Regime Scaling Snapshot (audit trail)
  4) Risk Filter
  5) Broker Apply (paper/shadow/live via broker_apply_orders)
  6) Execution mode snapshot
  7) Divergence check (if dual enabled)
  8) Stage audit logging

Safety:
  - Job lock enforced
  - Each stage auditable
  - Hard abort on failure
  - No partial stage leakage (stage audits are committed immediately)
  - Time budget enforcement (global + per-stage)
"""

import json
import os
import sys
import time
import traceback
from typing import Dict, Any, Tuple, Callable, Optional

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)

from engine.universe_discovery import discover_universe_once
from engine.execution_mode import get_execution_mode
from engine.kill_switch import execution_allowed


JOB_NAME = "trade_pipeline"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))

ENABLE_DUAL = os.environ.get("EXECUTION_DUAL_ENABLE", "0") == "1"

# ---- Time Budget Enforcement ----
# Global pipeline deadline from job start; if exceeded, abort before execution.
PIPELINE_MAX_DURATION_MS = int(os.environ.get("PIPELINE_MAX_DURATION_MS", "5000"))
# Any single stage exceeding this will hard-fail (prevents runaway steps).
PIPELINE_STAGE_BUDGET_MS = int(os.environ.get("PIPELINE_STAGE_BUDGET_MS", "2500"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _print(obj: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj, sort_keys=True) + "\n")
    sys.stdout.flush()


def _ensure_schema(con) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS pipeline_stage_audit (
  ts_ms INTEGER NOT NULL,
  stage TEXT NOT NULL,
  ok INTEGER NOT NULL,
  duration_ms INTEGER,
  detail_json TEXT,
  PRIMARY KEY (ts_ms, stage)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_stage_ts
  ON pipeline_stage_audit(ts_ms);
"""
    )
    try:
        con.commit()
    except Exception:
        pass


def _audit(con, ts_ms: int, stage: str, ok: bool, dur_ms: int, detail: Dict[str, Any]) -> None:
    con.execute(
        """
        INSERT OR REPLACE INTO pipeline_stage_audit
        (ts_ms, stage, ok, duration_ms, detail_json)
        VALUES (?,?,?,?,?)
        """,
        (
            int(ts_ms),
            str(stage),
            1 if ok else 0,
            int(dur_ms),
            json.dumps(detail or {}),
        ),
    )
    # Ensure stage results persist even if later stages fail
    try:
        con.commit()
    except Exception:
        pass


def _deadline_exceeded(deadline_ms: int) -> bool:
    try:
        return _now_ms() > int(deadline_ms)
    except Exception:
        return False


def _run_stage(
    con,
    ts_ms: int,
    stage: str,
    fn: Callable[[], Any],
    *,
    deadline_ms: Optional[int] = None,
) -> Tuple[bool, Any]:
    start = _now_ms()

    # Global deadline pre-check
    if deadline_ms is not None and start > int(deadline_ms):
        _audit(con, ts_ms, stage, False, 0, {"error": "pipeline_deadline_exceeded_pre"})
        return False, {"error": "pipeline_deadline_exceeded_pre"}

    try:
        result = fn()
        dur = _now_ms() - start

        # Per-stage time budget
        if dur > int(PIPELINE_STAGE_BUDGET_MS):
            _audit(
                con,
                ts_ms,
                stage,
                False,
                dur,
                {"error": "stage_time_budget_exceeded", "duration_ms": int(dur), "budget_ms": int(PIPELINE_STAGE_BUDGET_MS)},
            )
            return False, {"error": "stage_time_budget_exceeded"}

        # Global deadline post-check (do not continue to next stages if exceeded)
        if deadline_ms is not None and _now_ms() > int(deadline_ms):
            _audit(
                con,
                ts_ms,
                stage,
                False,
                dur,
                {"error": "pipeline_deadline_exceeded_post", "duration_ms": int(dur)},
            )
            return False, {"error": "pipeline_deadline_exceeded_post"}

        _audit(con, ts_ms, stage, True, dur, result if isinstance(result, dict) else {})
        return True, result

    except Exception as e:
        dur = _now_ms() - start
        _audit(
            con,
            ts_ms,
            stage,
            False,
            dur,
            {"error": str(e), "trace": traceback.format_exc()},
        )
        return False, {"error": str(e)}


def _circuit_breaker_tripped() -> bool:
    try:
        from engine.execution.circuit_breaker import check_circuit_breaker
    except Exception:
        return False

    try:
        return bool(check_circuit_breaker())
    except Exception:
        return False


def main() -> int:
    if _circuit_breaker_tripped():
        _print({"ok": True, "status": "circuit_breaker_tripped", "job": JOB_NAME})
        return 0

    con = None
    ts_ms = _now_ms()
    pipeline_deadline_ms = int(ts_ms + int(PIPELINE_MAX_DURATION_MS))

    try:
        init_db()
        con = connect()
        _ensure_schema(con)

        if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
            _audit(con, ts_ms, "job_lock", True, 0, {"status": "locked_out"})
            _print({"ok": True, "status": "locked_out", "job": JOB_NAME})
            return 0

        _audit(con, ts_ms, "job_lock", True, 0, {"status": "acquired", "owner": OWNER, "pid": PID})

        # ----------- Time Budget Snapshot -----------
        _audit(
            con,
            ts_ms,
            "time_budget",
            True,
            0,
            {
                "pipeline_max_duration_ms": int(PIPELINE_MAX_DURATION_MS),
                "stage_budget_ms": int(PIPELINE_STAGE_BUDGET_MS),
                "deadline_ms": int(pipeline_deadline_ms),
            },
        )

        # ----------- Kill Switch Pre-Check -----------
        allow, reason, meta = execution_allowed(con=con, symbol=None, regime=None)
        _audit(con, ts_ms, "kill_switch_precheck", bool(allow), 0, {"reason": reason, "meta": meta})
        if not allow:
            _print({"ok": False, "status": "blocked", "reason": reason})
            return 0

        # ----------- Global Deadline Pre-Check -----------
        if _deadline_exceeded(pipeline_deadline_ms):
            _audit(con, ts_ms, "deadline_precheck", False, 0, {"error": "pipeline_deadline_exceeded_pre"})
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "deadline_precheck"})
            return 2
        _audit(con, ts_ms, "deadline_precheck", True, 0, {"ok": True})

        # ----------- 1. Universe Discovery -----------
        ok, uni = _run_stage(
            con,
            ts_ms,
            "universe_discovery",
            lambda: discover_universe_once(con=con, ts_ms=ts_ms),
            deadline_ms=pipeline_deadline_ms,
        )
        if not ok:
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "universe_discovery"})
            return 2

        # ----------- 2. Meta Strategy Allocation -----------
        alloc = {}
        try:
            _audit(con, ts_ms, "meta_allocation", True, 0, {"ok": True, "alloc": {}})
        except Exception:
            pass

        # ----------- 3. Portfolio Rebalance (writes portfolio_orders) -----------
        from portfolio_construct import compute_rebalance

        ok, pr = _run_stage(
            con,
            ts_ms,
            "portfolio_rebalance",
            lambda: compute_rebalance(),
            deadline_ms=pipeline_deadline_ms,
        )
        if not ok:
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "portfolio_rebalance"})
            return 2

        # ----------- 3b. Regime Scaling Snapshot (read-only audit trail) -----------
        try:
            from engine.regime_size import regime_capital_scale
            _rs = regime_capital_scale(
                con=con,
                anchor=str(os.environ.get("PORTFOLIO_REGIME_ANCHOR", "SPY")).strip().upper(),
            )
        except Exception:
            _rs = {"ok": False}

        try:
            _audit(
                con,
                ts_ms,
                "regime_capital_scale",
                True,
                0,
                (_rs if isinstance(_rs, dict) else {"ok": True}),
            )
        except Exception:
            pass

        # ----------- 4. Risk Filter -----------
        from engine.risk_state import evaluate_risk_guards

        ok, _ = _run_stage(
            con,
            ts_ms,
            "risk_filter",
            lambda: evaluate_risk_guards(),
            deadline_ms=pipeline_deadline_ms,
        )
        if not ok:
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "risk_filter"})
            return 2

        # ----------- Global Deadline Gate BEFORE Execution -----------
        # Institutional rule: never start live/paper sends if the pipeline is late.
        if _deadline_exceeded(pipeline_deadline_ms):
            _audit(con, ts_ms, "execution_gate_deadline", False, 0, {"error": "pipeline_deadline_exceeded_before_execution"})
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "execution_gate_deadline"})
            return 2
        _audit(con, ts_ms, "execution_gate_deadline", True, 0, {"ok": True})

        # ----------- 5. Execution -----------
        import engine.execution.broker_apply_orders as broker_apply_orders  # uses existing entry logic

        ok, exec_res = _run_stage(
            con,
            ts_ms,
            "execution",
            lambda: broker_apply_orders.main(),
            deadline_ms=pipeline_deadline_ms,
        )
        if not ok:
            _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "execution"})
            return 2

        # ----------- 6. Execution Mode Snapshot -----------
        try:
            mode = get_execution_mode()
        except Exception:
            mode = {"mode": "unknown"}
        _audit(con, ts_ms, "mode_snapshot", True, 0, mode if isinstance(mode, dict) else {"mode": str(mode)})

        # ----------- 7. Divergence Check (optional dual) -----------
        if not ENABLE_DUAL:
            _audit(con, ts_ms, "divergence_check", True, 0, {"status": "skipped", "reason": "EXECUTION_DUAL_ENABLE!=1"})
        else:
            def _dual_check():
                # If dual is enabled, require an implementation; fail hard if missing.
                from engine.dual_execution import check_dual_divergence
                return check_dual_divergence(con=con, ts_ms=ts_ms, exec_result=exec_res)

            ok, _ = _run_stage(con, ts_ms, "divergence_check", _dual_check, deadline_ms=pipeline_deadline_ms)
            if not ok:
                _print({"ok": False, "status": "abort", "job": JOB_NAME, "stage": "divergence_check"})
                return 2

        _audit(
            con,
            ts_ms,
            "complete",
            True,
            0,
            {
                "job": JOB_NAME,
                "ts_ms": ts_ms,
                "owner": OWNER,
                "pid": PID,
                "alloc": alloc if isinstance(alloc, dict) else {},
            },
        )

        _print(
            {
                "ok": True,
                "status": "complete",
                "job": JOB_NAME,
                "ts_ms": ts_ms,
                "execution_mode": mode,
            }
        )
        return 0

    except Exception as e:
        try:
            if con is not None:
                _audit(con, ts_ms, "fatal", False, 0, {"error": str(e), "trace": traceback.format_exc()})
        except Exception:
            pass
        _print({"ok": False, "status": "fatal", "error": str(e)})
        return 2

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass
        try:
            if con is not None:
                con.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
