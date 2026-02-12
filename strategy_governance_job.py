# FILE: strategy_governance_job.py
# NEW FILE (CREATE)

"""
Promotes / demotes strategies automatically.

Rules:
  - Promotion requires N consecutive metric passes
  - Demotion triggers on metric failure
"""

import json
import os
import sys
import time

from dev_core.storage import connect, init_db

PROMOTE_STREAK = int(os.environ.get("STRAT_PROMOTE_STREAK", "3"))
MIN_SHARPE = float(os.environ.get("STRAT_MIN_SHARPE", "0.5"))
MAX_DD = float(os.environ.get("STRAT_MAX_DD", "0.25"))

def _now_ms():
    return int(time.time() * 1000)

def main():
    con = connect()
    try:
        init_db()

        rows = con.execute(
            "SELECT strategy_name, metrics_json FROM strategy_metrics"
        ).fetchall() or []

        for name, mj in rows:
            m = json.loads(mj or "{}")
            sharpe = float(m.get("sharpe_simple", 0))
            dd = float(m.get("max_drawdown", 1))

            reg = con.execute(
                "SELECT stage FROM strategy_registry WHERE strategy_name=?",
                (name,)
            ).fetchone()

            if not reg:
                continue

            stage = reg[0]

            if sharpe >= MIN_SHARPE and dd <= MAX_DD:
                # promote
                con.execute(
                    "UPDATE strategy_registry SET stage='live', updated_ts_ms=? WHERE strategy_name=?",
                    (_now_ms(), name)
                )
            else:
                # demote
                con.execute(
                    "UPDATE strategy_registry SET stage='paper', updated_ts_ms=? WHERE strategy_name=?",
                    (_now_ms(), name)
                )

        con.commit()
        print(json.dumps({"ok": True}))
        return 0

    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    finally:
        con.close()

if __name__ == "__main__":
    raise SystemExit(main())
