# FILE: decision_snapshot.py

import json
import time
from typing import Dict

from engine.dev_core.storage import connect, init_db
from engine.dev_core.execution_mode import get_execution_mode
from engine.dev_core.regime_stack import compute_regime_vector


def _now_ms() -> int:
    return int(time.time() * 1000)


def snapshot_decision(universe: Dict, allocation: Dict, portfolio: Dict):
    """
    Stores full decision snapshot including regime vectors per symbol.
    """

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
              execution_mode_json TEXT,
              regime_model_version TEXT,
              regime_vectors_json TEXT
            )
            """
        )

        ts = _now_ms()

        regime_vectors = {}
        try:
            for sym in (allocation or {}).keys():
                regime_vectors[sym] = compute_regime_vector(
                    symbol=sym,
                    ts_ms=ts,
                    con=con,
                )
        except Exception:
            regime_vectors = {}

        con.execute(
            """
            INSERT INTO trade_decision_snapshot(
            ts_ms,
            universe_json,
            allocation_json,
            portfolio_json,
            execution_mode_json,
            regime_model_version,
            regime_vectors_json
            )
            VALUES (?,?,?,?,?,?,?)

            """,
            (
                ts,
                json.dumps(universe, separators=(",", ":"), sort_keys=True),
                json.dumps(allocation, separators=(",", ":"), sort_keys=True),
                json.dumps(portfolio, separators=(",", ":"), sort_keys=True),
                json.dumps(get_execution_mode(), separators=(",", ":"), sort_keys=True),
                json.dumps(regime_vectors, separators=(",", ":"), sort_keys=True),
            ),
        )

        con.commit()

    finally:
        con.close()
