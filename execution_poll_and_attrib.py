"""
Poll broker fills + compute slippage metrics + pnl attribution snapshots.

Run every 60s (or 300s) in production.

Env:
  EXEC_POLL_LOOKBACK_S=3600
"""

import os
import time
import json
import logging

from dev_core.execution_ledger import (
    init_execution_ledger,
    compute_metrics_snapshot,
    compute_pnl_attribution_snapshot,
    compute_capital_efficiency_snapshot,
)
from dev_core.storage import connect
from dev_core.trade_attribution_ledger import (
    upsert_from_latest_pnl_attribution_snapshot,
    suppression_opportunity_snapshot,
)
from dev_core.pnl_decomposition_engine import compute_pnl_decomposition_snapshot

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [execution_poll_and_attrib] %(message)s",
)

POLL_LOOKBACK_S = int(os.environ.get("EXEC_POLL_LOOKBACK_S", "3600"))

# ------------------------------------------------------------
# Phase 2: Residual Hard Invariant (fail-closed)
# ------------------------------------------------------------
# Absolute $ guard (sum abs residual over latest snapshot)
RESIDUAL_ABS_PNL_MAX = float(os.environ.get("RESIDUAL_ABS_PNL_MAX", "50.0"))

# Ratio guard: sum(|residual|) / sum(|realized_pnl|) over latest snapshot
RESIDUAL_ABS_RATIO_MAX = float(os.environ.get("RESIDUAL_ABS_RATIO_MAX", "0.25"))

# If realized is near 0, use absolute guard only
RESIDUAL_REALIZED_EPS = float(os.environ.get("RESIDUAL_REALIZED_EPS", "1.0"))


def _residual_hard_invariant(*, snapshot_ts_ms: int) -> dict:
    resid_check = {"ok": True}
    snap_ts = int(snapshot_ts_ms or 0)
    if snap_ts <= 0:
        return resid_check

    con = connect(readonly=True)
    try:
        r = con.execute(
            """
            SELECT
              COUNT(1) AS n,
              SUM(ABS(COALESCE(residual_pnl,0))) AS sum_abs_resid,
              SUM(ABS(COALESCE(realized_pnl,0))) AS sum_abs_realized
            FROM pnl_decomposition
            WHERE ts_ms=?
            """,
            (int(snap_ts),),
        ).fetchone()

        n = int(r[0] or 0) if r else 0
        sum_abs_resid = float(r[1] or 0.0) if r else 0.0
        sum_abs_realized = float(r[2] or 0.0) if r else 0.0

        ratio = None
        if sum_abs_realized >= float(RESIDUAL_REALIZED_EPS):
            ratio = float(sum_abs_resid) / float(sum_abs_realized)

        resid_check = {
            "ok": True,
            "snapshot_ts_ms": int(snap_ts),
            "n": int(n),
            "sum_abs_residual": float(sum_abs_resid),
            "sum_abs_realized": float(sum_abs_realized),
            "ratio": (float(ratio) if ratio is not None else None),
            "abs_max": float(RESIDUAL_ABS_PNL_MAX),
            "ratio_max": float(RESIDUAL_ABS_RATIO_MAX),
            "realized_eps": float(RESIDUAL_REALIZED_EPS),
        }

        # Absolute guard always applies
        if float(sum_abs_resid) > float(RESIDUAL_ABS_PNL_MAX):
            resid_check["ok"] = False
            resid_check["failed"] = "abs_residual_exceeded"

        # Ratio guard applies only if realized sum is meaningful
        if ratio is not None and float(ratio) > float(RESIDUAL_ABS_RATIO_MAX):
            resid_check["ok"] = False
            resid_check["failed"] = "residual_ratio_exceeded"

        if not bool(resid_check["ok"]):
            raise RuntimeError(
                "RESIDUAL_INVARIANT_FAILED "
                f"failed={resid_check.get('failed')} "
                f"sum_abs_residual={sum_abs_resid:.6f} "
                f"sum_abs_realized={sum_abs_realized:.6f} "
                f"ratio={(ratio if ratio is not None else -1.0):.6f}"
            )

        return resid_check
    finally:
        try:
            con.close()
        except Exception:
            pass


def _orphan_pnl_invariant() -> dict:
    con = connect(readonly=True)
    try:
        orphan = con.execute(
            """
            SELECT COUNT(1)
            FROM pnl_attribution p
            LEFT JOIN trade_attribution_ledger t
              ON p.ts_ms = t.ts_ms
             AND p.source_alert_id = t.source_alert_id
             AND p.symbol = t.symbol
            WHERE t.id IS NULL
            """
        ).fetchone()[0]
        orphan = int(orphan or 0)
        if orphan > 0:
            raise RuntimeError(f"ATTRIBUTION_INCOMPLETE orphan_rows={orphan}")
        return {"ok": True, "orphan_rows": 0}
    finally:
        try:
            con.close()
        except Exception:
            pass


def main() -> int:
    init_execution_ledger()

    after_ts_ms = int(time.time() * 1000) - int(POLL_LOOKBACK_S) * 1000

    # Poll fills from each broker adapter that exists.
    try:
        from dev_core.broker_alpaca_rest import poll_and_log_fills
        poll_and_log_fills(after_ts_ms=after_ts_ms)
    except Exception:
        pass

    try:
        from dev_core.broker_ibkr_gateway import poll_and_log_fills as ibkr_poll
        ibkr_poll(after_ts_ms=after_ts_ms)
    except Exception:
        pass

    # Phase 2: manage open orders (cancel/replace) best-effort
    try:
        from dev_core.execution_microstructure import manage_open_orders
        manage_open_orders()
    except Exception:
        pass

    # Compute slippage + m2m metrics snapshot
    m = compute_metrics_snapshot(limit_orders=5000)

    # Compute pnl attribution snapshot (by signal/source_alert_id)
    a = compute_pnl_attribution_snapshot(lookback_orders=5000)

    # Capital efficiency snapshot (order + strategy aggregates)
    ce = compute_capital_efficiency_snapshot(limit_orders=5000)

    out = {"ok": True, "metrics": m, "attribution": a, "capital_efficiency": ce, "ts_ms": int(time.time() * 1000)}

    # Trade Attribution Ledger: enrich pnl_attribution with alerts + execution_policy_audit
    t = upsert_from_latest_pnl_attribution_snapshot()

    # Phase 2: PnL decomposition (alpha vs costs vs sizing vs residual)
    d = compute_pnl_decomposition_snapshot()

    # Residual Hard Invariant (latest snapshot only; fail-closed)
    resid_check = _residual_hard_invariant(snapshot_ts_ms=int((d or {}).get("snapshot_ts_ms") or 0))

    # Hard invariant: every pnl row must have attribution row
    orphan_check = _orphan_pnl_invariant()

    # Suppression opportunity (counterfactual; best-effort)
    s = suppression_opportunity_snapshot(lookback_ms=24 * 60 * 60 * 1000)

    out = {
        "ok": True,
        "metrics": m,
        "attribution": a,
        "trade_attrib": t,
        "pnl_decomp": d,
        "residual_invariant": resid_check,
        "orphan_invariant": orphan_check,
        "suppression_opportunity": s,
        "ts_ms": int(time.time() * 1000),
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
