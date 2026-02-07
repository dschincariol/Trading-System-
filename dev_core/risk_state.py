# dev_core/risk_state.py
import time
from typing import Tuple

from dev_core.storage import connect, init_db


def _now_ms():
    return int(time.time() * 1000)


def set_state(key: str, value: str):
    init_db()
    con = connect()
    try:
        ok = 0 if had_error else 1

        con.execute(
            """
            INSERT OR REPLACE INTO risk_state(key, value, updated_ts_ms)
            VALUES (?,?,?)
            """,
            (str(key), str(value), _now_ms()),
        )
        con.commit()
    finally:
        con.close()


def get_state(key: str, default: str = "") -> str:
    init_db()
    con = connect()
    try:
        r = con.execute(
            "SELECT value FROM risk_state WHERE key=?",
            (str(key),),
        ).fetchone()
        return str(r[0]) if r else str(default)
    finally:
        con.close()


def get_state_row(key: str, default: str = "") -> Tuple[str, int]:
    """
    Returns (value, updated_ts_ms).
    """
    init_db()
    con = connect()
    try:
        r = con.execute(
            "SELECT value, updated_ts_ms FROM risk_state WHERE key=?",
            (str(key),),
        ).fetchone()
        if not r:
            return str(default), 0
        return str(r[0]), int(r[1] or 0)
    finally:
        con.close()
