# engine/runtime/locks.py
"""
Cross-process job locks + job history persistence.

Extracted from dashboard_server.py
"""

import os
import time
import threading

from engine.dev_core.storage import connect as _db_connect


# ---------------------------------------------------
# JOB LOCKS
# ---------------------------------------------------

def _ensure_job_locks():
    con = _db_connect()
    try:
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall()]
        except Exception:
            cols = []

        has_legacy_key = ("key" in cols) and ("job_name" not in cols)

        if has_legacy_key:
            try:
                con.execute("ALTER TABLE job_locks RENAME TO job_locks_legacy")
            except Exception:
                pass

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_locks (
              job_name TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              pid INTEGER NOT NULL,
              acquired_ts_ms INTEGER NOT NULL,
              heartbeat_ts_ms INTEGER NOT NULL,
              expires_ms INTEGER
            )
            """
        )

        con.commit()
    finally:
        con.close()


def acquire_lock(name: str, ttl_ms: int = 10_000) -> bool:
    _ensure_job_locks()
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))
        owner = f"{os.getpid()}:{threading.get_ident()}"
        pid = int(os.getpid())

        row = con.execute(
            "SELECT expires_ms FROM job_locks WHERE job_name=?",
            (str(name),),
        ).fetchone()

        if row:
            cur_exp = int(row[0] or 0)
            if cur_exp > now:
                return False

        con.execute(
            """
            INSERT OR REPLACE INTO job_locks
              (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
            VALUES (?,?,?,?,?,?)
            """,
            (str(name), owner, pid, now, now, exp),
        )
        con.commit()
        return True
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()


def release_lock(name: str):
    _ensure_job_locks()
    con = _db_connect()
    try:
        con.execute("DELETE FROM job_locks WHERE job_name=?", (str(name),))
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------
# JOB HISTORY
# ---------------------------------------------------

def _ensure_job_history():
    con = _db_connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              ts_ms INTEGER NOT NULL,
              job_name TEXT NOT NULL,
              event TEXT NOT NULL,
              detail TEXT,
              exit_code INTEGER
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_job_history_job_ts
              ON job_history(job_name, ts_ms)
            """
        )
        con.commit()
    finally:
        con.close()


def write_job_history(job_name: str, event: str, detail: str = "", exit_code: int = None):
    _ensure_job_history()
    con = _db_connect()
    try:
        ts_ms = int(time.time() * 1000)
        con.execute(
            """
            INSERT INTO job_history(ts_ms, job_name, event, detail, exit_code)
            VALUES (?,?,?,?,?)
            """,
            (ts_ms, job_name, event, detail, exit_code),
        )
        con.commit()
    finally:
        con.close()


def read_job_history(job_name: str, limit: int = 200):
    _ensure_job_history()
    con = _db_connect()
    try:
        rows = con.execute(
            """
            SELECT ts_ms, event, detail, exit_code
            FROM job_history
            WHERE job_name=?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (job_name, int(limit)),
        ).fetchall()

        return [
            {
                "ts_ms": int(r[0] or 0),
                "event": str(r[1] or ""),
                "detail": str(r[2] or ""),
                "exit_code": (int(r[3]) if r[3] is not None else None),
            }
            for r in rows or []
        ]
    finally:
        con.close()
