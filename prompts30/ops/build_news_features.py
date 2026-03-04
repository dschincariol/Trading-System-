"""
build_news_features.py

Aggregate symbol-level news impact scores into fixed buckets.  The pipeline
is responsible for:

1. Selecting all *prediction* rows whose underlying event timestamp falls in a
   given bucket.
2. Applying a regime-specific domain multiplier to each prediction.
3. Summing score = zscore * confidence * multiplier; counting hits.
4. Updating a rolling, exponentially decayed cumulative score per symbol.

The result is persisted in ``news_features`` (see schema in
engine/runtime/storage.py) and can be joined into training pipelines via the
existing factor feature framework (compute_factor_features.py).

This job is idempotent, as-of safe and uses the common job-lock/heartbeat
pattern used elsewhere in the repository.
"""

import os
import time
import json
import logging
from typing import Any, Dict, Tuple

from engine.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)
from engine.news_domain import domain_conf_multiplier, source_conf_multiplier
from engine.model_v2 import get_current_regime

JOB_NAME = "build_news_features"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15"))

BUCKET_SEC = int(os.environ.get("NEWS_BUCKET_SEC", "300"))          # 5min buckets
LOOKBACK_S = int(os.environ.get("NEWS_LOOKBACK_S", "86400"))        # 1 day
DECAY_ALPHA = float(os.environ.get("NEWS_DECAY_ALPHA", "0.80"))     # exponential decay
SLEEP_S = float(os.environ.get("NEWS_FEATURE_SLEEP_S", "30.0"))

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [build_news_features] %(message)s",
)
LOG = logging.getLogger("build_news_features")


def _bucket_start(ts_ms: int, bucket_sec: int) -> int:
    b = int(bucket_sec) * 1000
    if b <= 0:
        b = 300 * 1000
    return int(ts_ms // b) * b


def _compute_bucket(con, bucket_ts_ms: int, bucket_sec: int) -> Dict[str, Tuple[float, int]]:
    """Return mapping symbol -> (score_sum, count) for events in the bucket."""
    rows = con.execute(
        """
        SELECT p.symbol, p.horizon_s, p.predicted_z, p.confidence,
               e.ts_ms, e.meta_json
        FROM predictions p
        JOIN events e ON p.event_id = e.id
        WHERE e.ts_ms >= ? AND e.ts_ms < ?
        """,
        (int(bucket_ts_ms), int(bucket_ts_ms + bucket_sec * 1000)),
    ).fetchall()

    acc: Dict[str, Tuple[float, int]] = {}

    for sym, h, z, conf, et, meta in rows or []:
        try:
            score = float(z or 0.0) * float(conf or 0.0)
        except Exception:
            continue
        domain = ""
        source = ""
        try:
            m = json.loads(meta) if meta else {}
            domain = m.get("domain", "")
            source = m.get("source", "")
        except Exception:
            domain = ""
            source = ""
        mult = domain_conf_multiplier(domain, sym, get_current_regime(sym), int(h or 0))
        mult *= source_conf_multiplier(source, sym, get_current_regime(sym), int(h or 0))
        score *= float(mult)
        key = str(sym or "").upper().strip()
        if not key:
            continue
        s, n = acc.get(key, (0.0, 0))
        acc[key] = (s + score, n + 1)
    return acc


def main() -> None:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb = 0.0
    try:
        while True:
            con = connect()
            try:
                now_ms = int(time.time() * 1000)
                cutoff_ms = now_ms - int(LOOKBACK_S) * 1000

                b = _bucket_start(int(cutoff_ms), int(BUCKET_SEC))
                end = _bucket_start(int(now_ms), int(BUCKET_SEC))

                wrote = 0
                while b <= end:
                    if time.time() - last_hb >= HEARTBEAT_EVERY_S:
                        touch_job_lock(JOB_NAME, OWNER, PID)
                        put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"bucket_ts_ms": int(b)}))
                        last_hb = time.time()

                    bucket_scores = _compute_bucket(con, int(b), int(BUCKET_SEC))
                    for sym, (score_sum, cnt) in bucket_scores.items():
                        # compute decayed score
                        prev = con.execute(
                            "SELECT decayed_score FROM news_features WHERE symbol=? AND bucket_ts_ms=?",
                            (sym, int(b - BUCKET_SEC * 1000)),
                        ).fetchone()
                        prev_ds = float(prev[0]) if prev and prev[0] is not None else 0.0
                        decayed = prev_ds * float(DECAY_ALPHA) + float(score_sum)
                        con.execute(
                            """
                            INSERT OR REPLACE INTO news_features(
                              symbol, bucket_ts_ms, bucket_sec, score, decayed_score, count
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (sym, int(b), int(BUCKET_SEC), float(score_sum), float(decayed), int(cnt)),
                        )
                        wrote += 1

                    try:
                        con.commit()
                    except Exception:
                        pass

                    logging.info("buckets_upserted=%s", wrote)
                    b += int(BUCKET_SEC) * 1000

            finally:
                try:
                    con.close()
                except Exception:
                    pass

            time.sleep(float(SLEEP_S))
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
