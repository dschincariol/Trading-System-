# engine/runtime/shutdown.py
"""
Runtime shutdown utilities.

Goals (master prompt):
- Graceful stop of all jobs (no zombies)
- Flush SQLite WAL + close pooled connections
- Fail-safe (best-effort, never raises during shutdown)
"""

from __future__ import annotations

import time
from typing import Any, Optional


def runtime_shutdown(*, JOBS: Optional[Any] = None, SUPERVISOR: Optional[Any] = None) -> None:
    # Stop process jobs first (best-effort)
    try:
        if JOBS is not None:
            try:
                JOBS.stop_all()
            except Exception:
                pass
    except Exception:
        pass

    try:
        if SUPERVISOR is not None:
            try:
                SUPERVISOR.stop_all()
            except Exception:
                pass
    except Exception:
        pass

    # Give children a moment to exit before DB flush
    try:
        time.sleep(0.05)
    except Exception:
        pass

    # Flush WAL + close pooled connections (runtime owns storage)
    try:
        from engine.runtime.storage import connect, close_pooled_connections  # type: ignore
    except Exception:
        connect = None  # type: ignore
        close_pooled_connections = None  # type: ignore

    if connect is not None:
        try:
            con = connect(readonly=False)
            try:
                # best-effort: checkpoint + truncate to bound WAL
                con.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
            except Exception:
                try:
                    con.execute("PRAGMA wal_checkpoint(PASSIVE);").fetchall()
                except Exception:
                    pass
            try:
                con.commit()
            except Exception:
                pass
            try:
                con.close()
            except Exception:
                pass
        except Exception:
            pass

    if close_pooled_connections is not None:
        try:
            close_pooled_connections()
        except Exception:
            pass
