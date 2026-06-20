# engine/runtime/locks.py
"""
Cross-process job locks + job history persistence.

Single source of truth for:
- job_locks schema + migration
- acquire/touch/heartbeat/release
- job_history schema + retention
"""

import os
import time
import threading
from typing import Optional, List, Dict, Any

from engine.runtime.storage import connect as _db_connect


# ---------------------------------------------------
# JOB LOCKS
# ---------------------------------------------------

def ensure_job_locks() -> None:
    """
    Ensure job_locks table exists with expected columns.
    Handles legacy rename from (key, owner, expires_ms) -> job_locks(job_name,...)
    """
    con = _db_connect()
    try:
        # Detect whether job_locks exists and what columns it has
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        except Exception:
            cols = []

        # Legacy: job_locks existed but used "key" instead of "job_name"
        has_legacy_key = ("key" in cols) and ("job_name" not in cols)

        if has_legacy_key:
            try:
                con.execute("ALTER TABLE job_locks RENAME TO job_locks_legacy")
            except Exception:
                pass

        # Create canonical table
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

        # Migrate legacy table if it exists
        try:
            legacy_cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks_legacy)").fetchall() or []]
        except Exception:
            legacy_cols = []

        if legacy_cols and ("key" in legacy_cols):
            now = int(time.time() * 1000)
            try:
                legacy_rows = con.execute(
                    "SELECT key, owner, expires_ms FROM job_locks_legacy"
                ).fetchall()
            except Exception:
                legacy_rows = []

            for k, owner, exp in legacy_rows or []:
                con.execute(
                    """
                    INSERT OR REPLACE INTO job_locks
                    (job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        str(k),
                        str(owner or ""),
                        0,
                        int(now),
                        int(now),
                        (int(exp) if exp is not None else None),
                    ),
                )

        # Ensure expected columns exist (defensive)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        except Exception:
            cols = []

        def _add(col: str, ddl: str) -> None:
            if col in cols:
                return
            try:
                con.execute(ddl)
            except Exception:
                pass

        _add("job_name", "ALTER TABLE job_locks ADD COLUMN job_name TEXT")
        _add("owner", "ALTER TABLE job_locks ADD COLUMN owner TEXT")
        _add("pid", "ALTER TABLE job_locks ADD COLUMN pid INTEGER")
        _add("acquired_ts_ms", "ALTER TABLE job_locks ADD COLUMN acquired_ts_ms INTEGER")
        _add("heartbeat_ts_ms", "ALTER TABLE job_locks ADD COLUMN heartbeat_ts_ms INTEGER")
        _add("expires_ms", "ALTER TABLE job_locks ADD COLUMN expires_ms INTEGER")

        con.commit()
    finally:
        con.close()

def acquire_lock(name: str, ttl_ms: int = 10_000) -> bool:
    """
    Acquire lock if absent or expired. Returns True if acquired.
    Atomic-ish for SQLite by using a conditional UPDATE then INSERT.
    """
    ensure_job_locks()
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))
        owner = f"{os.getpid()}:{threading.get_ident()}"
        pid = int(os.getpid())

        # Try to take over an existing expired lock
        cur = con.execute(
            """
            UPDATE job_locks
            SET owner=?, pid=?, acquired_ts_ms=?, heartbeat_ts_ms=?, expires_ms=?
            WHERE job_name=? AND (expires_ms IS NULL OR expires_ms <= ?)
            """,
            (str(owner), int(pid), int(now), int(now), int(exp), str(name), int(now)),
        )

        if cur.rowcount and cur.rowcount > 0:
            con.commit()
            return True

        # Otherwise try to insert a new lock (if it already exists and is not expired -> fail)
        try:
            con.execute(
                """
                INSERT INTO job_locks(job_name, owner, pid, acquired_ts_ms, heartbeat_ts_ms, expires_ms)
                VALUES (?,?,?,?,?,?)
                """,
                (str(name), str(owner), int(pid), int(now), int(now), int(exp)),
            )
            con.commit()
            return True
        except Exception:
            try:
                con.rollback()
            except Exception:
                pass
            return False

    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return False
    finally:
        con.close()

def touch_lock(name: str, ttl_ms: int = 10_000) -> None:
    """
    Extend expires_ms (no owner/pid mutation).
    """
    ensure_job_locks()
    con = _db_connect()
    try:
        now = int(time.time() * 1000)
        exp = int(now + int(ttl_ms))
        con.execute(
            "UPDATE job_locks SET expires_ms=? WHERE job_name=?",
            (int(exp), str(name)),
        )
        con.commit()
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
    finally:
        con.close()


def heartbeat_lock(name: str, ttl_ms: int = 60_000) -> None:
    """
    Update heartbeat_ts_ms (+ owner/pid) and extend expires_ms.
    """
    ensure_job_locks()

    # extend expiry first (best effort)
    try:
        touch_lock(name, ttl_ms=ttl_ms)
    except Exception:
        pass

    now = int(time.time() * 1000)
    owner = f"{os.getpid()}:{threading.get_ident()}"
    pid = int(os.getpid())

    con = _db_connect()
    try:
        # If column missing for some reason, degrade gracefully
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        except Exception:
            cols = []

        if "heartbeat_ts_ms" in cols:
            con.execute(
                "UPDATE job_locks SET heartbeat_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                (int(now), str(owner), int(pid), str(name)),
            )
        elif "acquired_ts_ms" in cols:
            con.execute(
                "UPDATE job_locks SET acquired_ts_ms=?, owner=?, pid=? WHERE job_name=?",
                (int(now), str(owner), int(pid), str(name)),
            )
        con.commit()
    finally:
        con.close()


def read_lock(name: str) -> Optional[Dict[str, Any]]:
    """
    Read job_locks row for a given lock name.
    Used by watchdog to detect silent stalls (heartbeat not advancing).
    """
    ensure_job_locks()
    con = _db_connect()
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(job_locks)").fetchall() or []]
        if "heartbeat_ts_ms" not in cols:
            return None
        row = con.execute(
            "SELECT job_name, owner, pid, expires_ms, heartbeat_ts_ms FROM job_locks WHERE job_name=?",
            (str(name),),
        ).fetchone()
        if not row:
            return None
        return {
            "job_name": row[0],
            "owner": row[1],
            "pid": row[2],
            "expires_ms": row[3],
            "heartbeat_ts_ms": row[4],
        }
    finally:
        con.close()

# ---------------------------------------------------
# JOB HISTORY
# ---------------------------------------------------

def ensure_job_history() -> None:
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


def write_job_history(
    job_name: str,
    event: str,
    detail: str = "",
    exit_code: Optional[int] = None,
    ts_ms: Optional[int] = None,
) -> None:
    """
    Append job history row. Enforces retention via JOB_HISTORY_MAX_ROWS.
    """
    try:
        ensure_job_history()
    except Exception:
        # history is non-critical
        pass

    con = _db_connect()
    try:
        now = int(ts_ms or (time.time() * 1000))
        con.execute(
            """
            INSERT INTO job_history(ts_ms, job_name, event, detail, exit_code)
            VALUES (?,?,?,?,?)
            """,
            (
                int(now),
                str(job_name or ""),
                str(event or ""),
                str(detail or ""),
                (int(exit_code) if exit_code is not None else None),
            ),
        )

        # retention
        try:
            max_rows = int(os.environ.get("JOB_HISTORY_MAX_ROWS", "20000"))
        except Exception:
            max_rows = 20000

        if max_rows > 0:
            con.execute(
                "DELETE FROM job_history WHERE id NOT IN (SELECT id FROM job_history ORDER BY ts_ms DESC LIMIT ?)",
                (int(max_rows),),
            )

        con.commit()
    finally:
        con.close()


def read_job_history(job_name: str, limit: int = 200) -> List[Dict[str, Any]]:
    ensure_job_history()
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
            (str(job_name or ""), int(limit)),
        ).fetchall()

        out: List[Dict[str, Any]] = []
        for ts_ms, event, detail, exit_code in rows or []:
            out.append(
                {
                    "ts_ms": int(ts_ms or 0),
                    "event": str(event or ""),
                    "detail": str(detail or ""),
                    "exit_code": (int(exit_code) if exit_code is not None else None),
                }
            )
        return out
    finally:
        con.close()


# ------------------------------------------------------------------
# Backward compatibility exports (required by runtime_bootstrap)
# ------------------------------------------------------------------

_ensure_job_locks = ensure_job_locks
_ensure_job_history = ensure_job_history