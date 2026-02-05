# kill_drift_monitor.py
"""
Auto-kill on model drift explosion using model_drift.drift_ratio.

Triggers:
- per-symbol kill if drift_ratio exceeds threshold
- global kill if too many symbols breach
"""

import os
import time
import json
from typing import Any, Dict, List, Tuple

from dev_core.storage import connect, init_db
from dev_core.kill_switch import activate


def _now_ms() -> int:
    return int(time.time() * 1000)


def main() -> int:
    init_db()
    con = connect()
    try:
        now_ms = _now_ms()

        min_n = int(os.environ.get("KILL_DRIFT_MIN_N", "30"))
        drift_ratio_thr = float(os.environ.get("KILL_DRIFT_RATIO_THR", "2.5"))
        global_breach_n = int(os.environ.get("KILL_DRIFT_GLOBAL_BREACH_N", "8"))

        rows = con.execute(
            """
            SELECT symbol, horizon_s, ts_ms, n, mae, baseline_mae, drift_ratio
            FROM model_drift
            """
        ).fetchall()

        breached: List[Tuple[str, int, float, int]] = []
        for sym, horizon_s, ts_ms, n, mae, baseline_mae, drift_ratio in rows:
            n_i = int(n or 0)
            if n_i < min_n:
                continue
            dr = float(drift_ratio or 0.0)
            if dr >= drift_ratio_thr:
                breached.append((str(sym), int(horizon_s or 0), float(dr), int(n_i)))
                try:
                    activate(
                        "symbol",
                        str(sym),
                        reason="auto_drift_explosion",
                        actor="system",
                        meta={
                            "symbol": str(sym),
                            "horizon_s": int(horizon_s or 0),
                            "n": int(n_i),
                            "drift_ratio": float(dr),
                            "drift_ratio_thr": float(drift_ratio_thr),
                            "mae": float(mae or 0.0),
                            "baseline_mae": float(baseline_mae or 0.0),
                            "ts_ms": int(ts_ms or 0),
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
                    reason="auto_drift_explosion_global",
                    actor="system",
                    meta={
                        "min_n": int(min_n),
                        "global_breach_n": int(global_breach_n),
                        "drift_ratio_thr": float(drift_ratio_thr),
                        "breached": [
                            {"symbol": s, "horizon_s": int(h), "drift_ratio": float(dr), "n": int(n)}
                            for (s, h, dr, n) in breached[:80]
                        ],
                    },
                    action="AUTO",
                    con=con,
                )
            except Exception:
                pass

        out: Dict[str, Any] = {
            "ok": True,
            "min_n": int(min_n),
            "drift_ratio_thr": float(drift_ratio_thr),
            "breach_count": int(len(breached)),
            "breached": [
                {"symbol": s, "horizon_s": int(h), "drift_ratio": float(dr), "n": int(n)}
                for (s, h, dr, n) in breached[:80]
            ],
            "ts_ms": int(now_ms),
        }
        print(json.dumps(out, indent=2))
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
