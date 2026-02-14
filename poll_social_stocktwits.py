
"""
poll_social_stocktwits.py

Ingest social messages from StockTwits (best-effort, safe defaults).

Writes to SQLite:
- social_posts

This job is non-critical: failures must not stop the rest of the system.
"""

import hashlib
import json
import os
import time
import logging
from typing import Any, Dict, List, Optional

import requests

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

_REGION_MAP_CACHE = None

JOB_NAME = "poll_social_stocktwits"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [poll_social_stocktwits] %(message)s",
)

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

# Public trending stream (no auth). This is the safest default.
ST_TRENDING_URL = os.environ.get("STOCKTWITS_TRENDING_URL", "https://api.stocktwits.com/api/2/streams/trending.json")

# Optional: attempt symbol streams (may require partner access)
ST_SYMBOL_URL_TMPL = os.environ.get(
    "STOCKTWITS_SYMBOL_URL_TMPL",
    "https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json",
)

ST_TIMEOUT_S = float(os.environ.get("STOCKTWITS_TIMEOUT_S", "10.0"))
ST_SLEEP_S = float(os.environ.get("SOCIAL_POLL_SLEEP_S", "30.0"))

HASH_SALT = os.environ.get("SOCIAL_HASH_SALT", "social")


def _sha(s: str) -> str:
    h = hashlib.sha256()
    h.update((HASH_SALT + "|" + str(s)).encode("utf-8", "ignore"))
    return h.hexdigest()


def _parse_ts_ms(created_at: str) -> int:
    # StockTwits created_at is ISO 8601, e.g. 2020-01-01T12:34:56Z
    try:
        t = time.strptime(str(created_at).replace("Z", "UTC"), "%Y-%m-%dT%H:%M:%S%Z")
        return int(time.mktime(t) * 1000)
    except Exception:
        return int(time.time() * 1000)


def _safe_int(x) -> Optional[int]:
    try:
        if x is None:
            return None
        return int(x)
    except Exception:
        return None


def _safe_get_msg_symbols(msg: Dict[str, Any]) -> List[str]:
    out = []
    try:
        syms = msg.get("symbols") or []
        for s in syms:
            sym = str((s or {}).get("symbol") or "").upper().strip()
            if sym:
                out.append(sym)
    except Exception:
        pass
    return out


def _fetch_json(url: str) -> Optional[Dict[str, Any]]:
    try:
        r = requests.get(url, timeout=float(ST_TIMEOUT_S))
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _insert_message(con, *, platform: str, symbol: str, msg: Dict[str, Any]) -> None:
    post_id = str(msg.get("id") or "")
    if not post_id:
        return

    created_at = msg.get("created_at")
    ts_ms = _parse_ts_ms(str(created_at)) if created_at else int(time.time() * 1000)

    body = str(msg.get("body") or "")
    user = msg.get("user") or {}

    author_id = user.get("id")
    author_hash = _sha(str(author_id)) if author_id is not None else None

    like_count = _safe_int(msg.get("likes") or msg.get("like_count"))
    reply_count = _safe_int(msg.get("replies") or msg.get("reply_count"))
    repost_count = _safe_int(msg.get("reshares") or msg.get("repost_count"))
    quote_count = _safe_int(msg.get("quotes") or msg.get("quote_count"))
    follower_count = _safe_int(user.get("followers"))

    con.execute(
        """
        INSERT OR IGNORE INTO social_posts(
          ts_ms, platform, symbol, post_id, author_id_hash,
          text, lang,
          like_count, reply_count, repost_count, quote_count,
          follower_count, is_spam, spam_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL)
        """,
        (
            int(ts_ms),
            str(platform),
            str(symbol),
            str(post_id),
            author_hash,
            body,
            None,
            like_count,
            reply_count,
            repost_count,
            quote_count,
            follower_count,
        ),
    )


def _poll_once(con) -> int:
    n = 0

    payload = _fetch_json(str(ST_TRENDING_URL)) or {}
    msgs = payload.get("messages") or []
    for msg in msgs:
        syms = _safe_get_msg_symbols(msg)
        for sym in syms:
            try:
                _insert_message(con, platform="stocktwits", symbol=sym, msg=msg)
                n += 1
            except Exception:
                pass

    return int(n)


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=int(LOCK_STALE_AFTER_S)):
        logging.info("lock not acquired; exiting")
        return

    con = connect()
    try:
        last_hb_s = 0.0
        while True:
            now_s = time.time()
            if (now_s - last_hb_s) >= float(HEARTBEAT_EVERY_S):
                try:
                    touch_job_lock(JOB_NAME, OWNER, PID)
                    put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"stage": "poll"}))
                except Exception:
                    pass
                last_hb_s = now_s

            try:
                n = _poll_once(con)
                con.commit()
                if n:
                    logging.info("ingested=%d rows", int(n))
            except Exception as e:
                try:
                    con.rollback()
                except Exception:
                    pass
                logging.warning("poll error: %s", str(e))

            time.sleep(float(ST_SLEEP_S))
    finally:
        try:
            con.close()
        except Exception:
            pass
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
