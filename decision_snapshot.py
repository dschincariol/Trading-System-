# FILE: decision_snapshot.py
# NEW FILE (CREATE)

import json
import time
from dev_core.storage import connect, init_db
from dev_core.execution_mode import get_execution_mode

def snapshot_decision(universe, allocation, portfolio):
    con = connect()
    try:
        init_db()
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS trade_decision_snapshot(
              ts_ms INTEGER PRIMARY KEY,
              universe_json TEXT,
              allocation_json TEXT,
              portfolio_json TEXT,
              execution_mode_json TEXT
            )
            """
        )
        ts = int(time.time() * 1000)
        con.execute(
            "INSERT INTO trade_decision_snapshot VALUES (?,?,?,?,?)",
            (
                ts,
                json.dumps(universe),
                json.dumps(allocation),
                json.dumps(portfolio),
                json.dumps(get_execution_mode())
            )
        )
        con.commit()
    finally:
        con.close()
