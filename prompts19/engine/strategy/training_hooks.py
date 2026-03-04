# dev_core/training_hooks.py
"""
Lightweight instrumentation for adaptive retraining triggers.

This module provides a simple SQLite-backed queue of retraining requests
that can be written by drift/regime/quality checks and read by
scheduled jobs (e.g. train_embed_models or train_model_v2).

The design is intentionally minimal:
  * idempotent writes (duplicate signals collapse)
  * optional `processed` flag for housekeeping
  * basic helpers for checking thresholds

No external dependencies beyond engine.storage.
"""

import time
from typing import List, Tuple, Optional

from engine.storage import connect, init_db


TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS retraining_signals (
    model_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(model_name, reason)
);
CREATE INDEX IF NOT EXISTS idx_retraining_signals_ts ON retraining_signals(ts_ms);
"""



def _now_ms() -> int:
    return int(time.time() * 1000)


def init_hooks_db(con=None) -> None:
    init_db()
    close = False
    if con is None:
        con = connect()
        close = True
    try:
        con.executescript(TABLE_SCHEMA)
        con.commit()
    finally:
        if close:
            con.close()



def schedule_retraining(model_name: str, reason: str) -> None:
    """Insert or update a retraining signal. Duplicate reasons collapse."""
    init_hooks_db()
    con = connect()
    try:
        now = _now_ms()
        con.execute(
            """
            INSERT OR REPLACE INTO retraining_signals(model_name, reason, ts_ms, processed)
            VALUES (?,?,?,0)
            """,
            (str(model_name), str(reason), int(now)),
        )
        con.commit()
    finally:
        con.close()



def fetch_pending(model_name: Optional[str] = None) -> List[Tuple[str, str, int]]:
    """Return list of (model_name, reason, ts_ms) signals that are unprocessed."""
    init_hooks_db()
    con = connect()
    try:
        if model_name:
            rows = con.execute(
                """
                SELECT model_name, reason, ts_ms
                FROM retraining_signals
                WHERE processed=0 AND model_name=?
                ORDER BY ts_ms ASC
                """,
                (str(model_name),),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT model_name, reason, ts_ms
                FROM retraining_signals
                WHERE processed=0
                ORDER BY ts_ms ASC
                """,
            ).fetchall()
        return [(str(r[0]), str(r[1]), int(r[2])) for r in rows or []]
    finally:
        con.close()



def mark_processed(model_name: str, reason: str) -> None:
    init_hooks_db()
    con = connect()
    try:
        con.execute(
            """
            UPDATE retraining_signals
            SET processed=1
            WHERE model_name=? AND reason=?
            """,
            (str(model_name), str(reason)),
        )
        con.commit()
    finally:
        con.close()
