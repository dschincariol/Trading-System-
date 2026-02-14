"""
poll_social_reddit.py

Selective Reddit ingestion for trading signals.

- Targets finance-relevant subreddits only
- Stores raw posts/comments into social_posts
- Uses same schema + safety guarantees as StockTwits
- Read-only, non-critical job
"""

import os
import time
import json
import hashlib
import logging
from typing import Any, Dict, List

import praw

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

JOB_NAME = "poll_social_reddit"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [poll_social_reddit] %(message)s",
)

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15"))

REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.environ.get("REDDIT_USER_AGENT", "market-research-bot")

SUBREDDITS = os.environ.get(
    "REDDIT_SUBREDDITS",
    "wallstreetbets,stocks,investing,options,cryptocurrency,ethtrader",
).split(",")

POLL_LIMIT = int(os.environ.get("REDDIT_POLL_LIMIT", "50"))
SLEEP_S = float(os.environ.get("SOCIAL_POLL_SLEEP_S", "60"))

HASH_SALT = os.environ.get("SOCIAL_HASH_SALT", "social")


def _sha(x: str) -> str:
    h = hashlib.sha256()
    h.update((HASH_SALT + "|" + str(x)).encode("utf-8", "ignore"))
    return h.hexdigest()


def _extract_symbols(text: str) -> List[str]:
    if not text:
        return []
    out = set()
    for tok in text.replace("$", " $").split():
        if tok.startswith("$") and tok[1:].isalpha() and 1 <= len(tok[1:]) <= 5:
            out.add(tok[1:].upper())
    return list(out)


def main():
    if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
        logging.warning("reddit credentials missing; exiting")
        return

    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.info("lock not acquired; exiting")
        return

    reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
    )

    con = connect()
    try:
        last_hb = 0.0

        while True:
            now_s = time.time()
            if now_s - last_hb >= HEARTBEAT_EVERY_S:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"stage": "poll"}))
                last_hb = now_s

            for sr in SUBREDDITS:
                try:
                    subreddit = reddit.subreddit(sr.strip())
                    for post in subreddit.new(limit=POLL_LIMIT):
                        syms = _extract_symbols(f"{post.title} {post.selftext}")
                        if not syms:
                            continue

                        ts_ms = int(post.created_utc * 1000)
                        author_hash = _sha(post.author.name) if post.author else None

                        for sym in syms:
                            con.execute(
                                """
                                INSERT OR IGNORE INTO social_posts(
                                  ts_ms, platform, symbol, post_id, author_id_hash,
                                  text, lang,
                                  like_count, reply_count, repost_count, quote_count,
                                  follower_count, is_spam, spam_reason
                                )
                                VALUES (?, 'reddit', ?, ?, ?, ?, 'en', ?, ?, NULL, NULL, NULL, 0, NULL)
                                """,
                                (
                                    ts_ms,
                                    sym,
                                    f"t3_{post.id}",
                                    author_hash,
                                    f"{post.title}\n{post.selftext}",
                                    int(post.score or 0),
                                    int(post.num_comments or 0),
                                ),
                            )
                except Exception as e:
                    logging.warning("subreddit error %s: %s", sr, e)

            con.commit()
            time.sleep(SLEEP_S)

    finally:
        con.close()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
