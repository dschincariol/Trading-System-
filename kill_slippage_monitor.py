# kill_slippage_monitor.py
"""
Auto-kill on execution cost / slippage explosion using labels_exec (net-of-cost).

Triggers:
- per-symbol kill if avg_total_cost_bps over recent window exceeds threshold
- global kill if too many symbols breach in same window
"""

import os
import time
import json
from typing import Dict, Any, List, Tuple

from engine.dev_core.storage import connect, init_db
from engine.dev_core.kill_switch import activate


def _now_ms() -> int:
    return int(time.time() * 1000)


def main() -> int:
    init_db()
    con = connect()
    try:
        now_ms = _now_ms()

        lookback_s = int(os.environ.get("KILL_SLIPPAGE_LOOKBACK_S", "7200"))  # 2h
        min_n = int(os.environ.get("KILL_SLIPPAGE_MIN_N", "8"))
        max_total_cost_bps = float(os.environ.get("KILL_SLIPPAGE_MAX_TOTAL_COST_BPS", "50.0"))
        global_breach_n = int(os.environ.get("KILL_SLIPPAGE_GLOBAL_BREACH_N", "5"))

        since_ms = int(now_ms - lookback_s * 1000)

        rows = con.execute(
            """
            SELECT symbol,
                   COUNT(*) AS n,
                   AVG(total_cost_bps) AS avg_cost_bps,
                   AVG(slippage_bps) AS avg_slip_bps,
                   AVG(fees_bps) AS avg_fee_bps,
                   AVG(spread_bps) AS avg_spread_bps
            FROM labels_exec
            WHERE ts_ms >= ?
              AND realized = 1
            GROUP BY symbol
            """,
            (since_ms,),
        ).fetchall()

        breached: List[Tuple[str, int, float]] = []
        for sym, n, avg_cost, avg_slip, avg_fee, avg_spread in rows:
            n_i = int(n or 0)
            if n_i < min_n:
                continue
            avg_cost_f = float(avg_cost or 0.0)
            if avg_cost_f >= max_total_cost_bps:
                breached.append((str(sym), n_i, avg_cost_f))
                try:
                    activate(
                        "symbol",
                        str(sym),
                        reason="auto_slippage_explosion",
                        actor="system",
                        meta={
                            "window_s": int(lookback_s),
                            "min_n": int(min_n),
                            "n": int(n_i),
                            "avg_total_cost_bps": float(avg_cost_f),
                            "max_total_cost_bps": float(max_total_cost_bps),
                            "avg_slippage_bps": float(avg_slip or 0.0),
                            "avg_fees_bps": float(avg_fee or 0.0),
                            "avg_spread_bps": float(avg_spread or 0.0),
                        },
                        action="AUTO",
                        con=con,
                    )
                except Exception:
                    pass

        if len(breached) >= global_breach_n:
            try:
                activate(
                    "global",
                    "global",
                    reason="auto_slippage_explosion_global",
                    actor="system",
                    meta={
                        "window_s": int(lookback_s),
                        "min_n": int(min_n),
                        "global_breach_n": int(global_breach_n),
                        "breached": [
                            {"symbol": s, "n": int(n), "avg_total_cost_bps": float(c)}
                            for (s, n, c) in breached[:50]
                        ],
                        "max_total_cost_bps": float(max_total_cost_bps),
                    },
                    action="AUTO",
                    con=con,
                )
            except Exception:
                pass

        out: Dict[str, Any] = {
            "ok": True,
            "window_s": int(lookback_s),
            "min_n": int(min_n),
            "max_total_cost_bps": float(max_total_cost_bps),
            "breach_count": int(len(breached)),
            "breached": [
                {"symbol": s, "n": int(n), "avg_total_cost_bps": float(c)}
                for (s, n, c) in breached[:50]
            ],
            "ts_ms": int(now_ms),
        }
        print(json.dumps(out, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
