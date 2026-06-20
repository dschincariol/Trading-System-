# NEW FILE: engine/runtime/runtime_meta.py
# CREATE THIS FILE EXACTLY:

import time
from typing import Optional, Dict

from engine.runtime.storage import connect as _db_connect


def _ensure_runtime_meta(con) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_meta (
          key TEXT PRIMARY KEY,
          value TEXT,
          updated_ts_ms INTEGER
        )
        """
    )


def meta_get(key: str, default: Optional[str] = None) -> Optional[str]:
    con = _db_connect(readonly=False)
    try:
        _ensure_runtime_meta(con)
        row = con.execute("SELECT value FROM runtime_meta WHERE key=?", (str(key),)).fetchone()
        return str(row[0]) if row and row[0] is not None else default
    finally:
        try:
            con.close()
        except Exception:
            pass


def meta_set(key: str, value: str) -> None:
    con = _db_connect(readonly=False)
    try:
        _ensure_runtime_meta(con)
        now = int(time.time() * 1000)
        con.execute(
            """
            INSERT INTO runtime_meta(key, value, updated_ts_ms)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (str(key), str(value), int(now)),
        )
        con.commit()
    finally:
        try:
            con.close()
        except Exception:
            pass


def meta_set_if_missing(key: str, value: str) -> bool:
    """
    Returns True if it set the value, False if already present.
    """
    con = _db_connect(readonly=False)
    try:
        _ensure_runtime_meta(con)
        row = con.execute("SELECT value FROM runtime_meta WHERE key=?", (str(key),)).fetchone()
        if row and row[0] is not None and str(row[0]) != "":
            return False
        now = int(time.time() * 1000)
        con.execute(
            """
            INSERT INTO runtime_meta(key, value, updated_ts_ms)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET
              value=excluded.value,
              updated_ts_ms=excluded.updated_ts_ms
            """,
            (str(key), str(value), int(now)),
        )
        con.commit()
        return True
    finally:
        try:
            con.close()
        except Exception:
            pass