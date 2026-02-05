# evaluate_strategies.py
"""
C1-2: Strategy evaluator (writes strategy_metrics via portfolio_backtest).

Runs:
- baseline
- conservative

No architecture changes: uses portfolio_backtest.py as the harness.
"""

import os
import json
import time
import math
import logging
from typing import List, Dict, Any

from dev_core.storage import connect, init_db

import portfolio_backtest


# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [evaluate_strategies] %(message)s",
)


# ------------------------------------------------------------
# Metrics helpers
# ------------------------------------------------------------

def _sharpe(returns: List[float], eps: float = 1e-9) -> float:
    if not returns:
        return 0.0
    mu = sum(returns) / len(returns)
    var = sum((r - mu) ** 2 for r in returns) / max(1, (len(returns) - 1))
    sd = math.sqrt(max(var, eps))
    return mu / sd if sd > 0 else 0.0


def _sortino(returns: List[float], eps: float = 1e-9) -> float:
    if not returns:
        return 0.0
    mu = sum(returns) / len(returns)
    neg = [r for r in returns if r < 0]
    if not neg:
        return mu / eps
    var = sum(r ** 2 for r in neg) / max(1, len(neg))
    sd = math.sqrt(max(var, eps))
    return mu / sd if sd > 0 else 0.0


def _max_drawdown(equity: List[float]) -> float:
    peak = None
    max_dd = 0.0
    for e in equity:
        if peak is None or e > peak:
            peak = e
        if peak is not None:
            max_dd = min(max_dd, (e - peak))
    return abs(max_dd)


# ------------------------------------------------------------
# Strategy runner
# ------------------------------------------------------------

def _run(strategy: str) -> Dict[str, Any]:
    os.environ["BT_STRATEGY"] = str(strategy)
    res = portfolio_backtest.run_backtest()

    print(f"\n=== STRATEGY {strategy} ===")
    print(json.dumps(res.get("metrics", {}), indent=2))

    return res


# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

def main() -> int:
    init_db()

    con = connect()
    try:
        # Ensure schema
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_metrics (
              strategy TEXT NOT NULL,
              metrics_json TEXT NOT NULL,
              ts_ms INTEGER NOT NULL
            )
            """
        )
        con.commit()

        for name in ("baseline", "conservative"):
            res = _run(name)

            points = res.get("points", []) or []
            returns = [float(x.get("ret", 0.0)) for x in points]
            equity = [float(x.get("equity", 0.0)) for x in points]

            pnl = sum(returns)
            sharpe = _sharpe(returns)
            sortino = _sortino(returns)
            max_dd = _max_drawdown(equity)

            metrics = {
                "pnl": float(pnl),
                "sharpe": float(sharpe),
                "sortino": float(sortino),
                "max_drawdown": float(max_dd),
                "n_points": int(len(points)),
            }

            con.execute(
                """
                INSERT INTO strategy_metrics(strategy, metrics_json, ts_ms)
                VALUES (?, ?, ?)
                """,
                (str(name), json.dumps(metrics), int(time.time() * 1000)),
            )
            con.commit()

            logging.info(
                "STRATEGY %s pnl=%.4f sharpe=%.3f sortino=%.3f max_dd=%.4f",
                str(name),
                float(pnl),
                float(sharpe),
                float(sortino),
                float(max_dd),
            )

        print("\nDONE: strategy_metrics updated (see dev.db strategy_metrics table)")
        return 0

    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
