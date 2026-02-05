# dev_core/symbol_blacklist.py
"""
Symbol blacklist with SQLite persistence.

- Stores symbols temporarily/permanently blacklisted due to live (or sim) performance.
- Portfolio layer can consult this to skip symbols.

Tables:
  symbol_blacklist(symbol PRIMARY KEY, until_ts_ms, reason, score, meta_json)
"""

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from dev_core.storage import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS symbol_blacklist (
  symbol TEXT PRIMARY KEY,
  until_ts_ms INTEGER,
  reason TEXT NOT NULL,
  score REAL NOT NULL DEFAULT 0,
  meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_symbol_blacklist_until
  ON symbol_blacklist(until_ts_ms);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def init_blacklist() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def is_blacklisted(con, symbol: str, now_ms: Optional[int] = None) -> bool:
    init_blacklist()
    if now_ms is None:
        now_ms = _now_ms()

    row = con.execute(
        """
        SELECT until_ts_ms
        FROM symbol_blacklist
        WHERE symbol=?
        """,
        (str(symbol).upper().strip(),),
    ).fetchone()

    if not row:
        return False

    until_ts = row[0]
    if until_ts is None:
        return True  # permanent
    try:
        return int(until_ts) > int(now_ms)
    except Exception:
        return True


def get_blacklisted_symbols(con, now_ms: Optional[int] = None, limit: int = 5000) -> List[str]:
    init_blacklist()
    if now_ms is None:
        now_ms = _now_ms()

    rows = con.execute(
        """
        SELECT symbol, until_ts_ms
        FROM symbol_blacklist
        ORDER BY COALESCE(until_ts_ms, 9223372036854775807) DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()

    out = []
    for sym, until_ts in rows or []:
        try:
            sym = str(sym).upper().strip()
            if not sym:
                continue
            if until_ts is None:
                out.append(sym)
                continue
            if int(until_ts) > int(now_ms):
                out.append(sym)
        except Exception:
            continue
    return out


def upsert_blacklist(
    con,
    symbol: str,
    reason: str,
    score: float,
    ttl_s: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    init_blacklist()
    sym = str(symbol).upper().strip()
    until_ts = None
    if ttl_s is not None:
        until_ts = _now_ms() + int(ttl_s) * 1000

    con.execute(
        """
        INSERT INTO symbol_blacklist(symbol, until_ts_ms, reason, score, meta_json)
        VALUES (?,?,?,?,?)
        ON CONFLICT(symbol) DO UPDATE SET
          until_ts_ms=excluded.until_ts_ms,
          reason=excluded.reason,
          score=excluded.score,
          meta_json=excluded.meta_json
        """,
        (sym, int(until_ts) if until_ts is not None else None, str(reason), float(score), json.dumps(meta or {}, separators=(",", ":"), sort_keys=True)),
    )


def clear_blacklist(con, symbol: str) -> None:
    init_blacklist()
    con.execute("DELETE FROM symbol_blacklist WHERE symbol=?", (str(symbol).upper().strip(),))
