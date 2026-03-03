# FILE: live_stability_guard_job.py
# NEW FILE (CREATE)

"""
Live Stability Guard

Hard protection independent of strategy metrics.

Guards:
  - Rolling equity drawdown
  - Max daily loss
  - Turnover spike
  - Slippage drift

On breach:
  set_execution_armed(0)
  set_execution_mode("paper")
"""

import json
import os
import sys
import time
import math

from engine.storage import connect, init_db
from engine.execution_mode import set_execution_mode, set_execution_armed

MAX_DD = float(os.environ.get("LIVE_MAX_DRAWDOWN", "0.25"))
MAX_DAILY_LOSS = float(os.environ.get("LIVE_MAX_DAILY_LOSS", "0.05"))
MAX_TURNOVER = float(os.environ.get("LIVE_MAX_TURNOVER", "2.0"))
MAX_SLIPPAGE_DRIFT = float(os.environ.get("LIVE_MAX_SLIPPAGE_DRIFT", "0.02"))

def _now_ms():
    return int(time.time() * 1000)

def _print(x):
    sys.stdout.write(json.dumps(x, sort_keys=True) + "\n")
    sys.stdout.flush()

def main():
    con = connect()
    try:
        init_db()

        # equity curve
        rows = con.execute(
            "SELECT ts_ms, equity FROM portfolio_equity ORDER BY ts_ms ASC"
        ).fetchall() or []

        if not rows:
            _print({"ok": True, "status": "no_equity_data"})
            return 0

        eq = [float(r[1]) for r in rows]
        peak = None
        max_dd = 0.0
        for v in eq:
            if peak is None or v > peak:
                peak = v
            dd = (peak - v) / peak if peak and peak > 0 else 0.0
            max_dd = max(max_dd, dd)

        # daily loss
        today = int(_now_ms() // 86400000)
        day_rows = con.execute(
            "SELECT pnl FROM pnl_attribution WHERE ts_ms >= ?",
            (today * 86400000,),
        ).fetchall() or []
        day_pnl = sum(float(r[0] or 0.0) for r in day_rows)

        # turnover
        turn_rows = con.execute(
            "SELECT delta_weight FROM portfolio_orders WHERE ts_ms >= ?",
            (today * 86400000,),
        ).fetchall() or []
        turnover = sum(abs(float(r[0] or 0.0)) for r in turn_rows)

        # slippage drift
        slip_rows = con.execute(
            "SELECT expected_px, fill_px FROM execution_fills WHERE ts_ms >= ?",
            (today * 86400000,),
        ).fetchall() or []
        drift = 0.0
        if slip_rows:
            diffs = []
            for e, f in slip_rows:
                if e and f:
                    diffs.append(abs(float(f) - float(e)) / float(e))
            drift = sum(diffs) / len(diffs) if diffs else 0.0

        breach = (
            max_dd > MAX_DD
            or abs(day_pnl) > MAX_DAILY_LOSS
            or turnover > MAX_TURNOVER
            or drift > MAX_SLIPPAGE_DRIFT
        )

        if breach:
            set_execution_armed(0, actor="stability_guard", reason="risk_breach")
            set_execution_mode("paper", actor="stability_guard", reason="risk_breach")

        _print({
            "ok": True,
            "breach": breach,
            "max_dd": max_dd,
            "daily_pnl": day_pnl,
            "turnover": turnover,
            "slippage_drift": drift
        })
        return 0

    except Exception as e:
        _print({"ok": False, "error": str(e)})
        return 2
    finally:
        con.close()

if __name__ == "__main__":
    raise SystemExit(main())
