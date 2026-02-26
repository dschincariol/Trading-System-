import os
import time
import json
import math
import logging
import statistics
from typing import Any, Dict, List, Tuple

from engine.runtime.storage import (
    init_db,
    connect,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

JOB_NAME = "compute_gdelt_macro"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "10.0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

BUCKET_SEC = int(os.environ.get("GDELT_MACRO_BUCKET_SEC", "900"))          # 15m
LOOKBACK_S = int(os.environ.get("GDELT_MACRO_LOOKBACK_S", "21600"))        # 6h
SLEEP_S = float(os.environ.get("GDELT_MACRO_SLEEP_S", "60.0"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [compute_gdelt_macro] %(message)s",
)


def _bucket_start(ts_ms: int, bucket_sec: int) -> int:
    b = int(bucket_sec) * 1000
    if b <= 0:
        b = 900 * 1000
    return int(ts_ms // b) * b


def _is_conflict(meta: Dict[str, Any]) -> bool:
    gd = (meta or {}).get("gdelt") or {}
    themes = gd.get("themes") or []
    theme = str(gd.get("theme") or "").lower()
    # allow list: war, conflict, attacks, terrorism, sanctions, etc.
    keys = ("conflict", "war", "attack", "terror", "missile", "sanction", "military", "strike")
    if any(k in theme for k in keys):
        return True
    try:
        for t in themes:
            if any(k in str(t).lower() for k in keys):
                return True
    except Exception:
        pass
    return False


def _is_econ(meta: Dict[str, Any]) -> bool:
    gd = (meta or {}).get("gdelt") or {}
    themes = gd.get("themes") or []
    theme = str(gd.get("theme") or "").lower()
    keys = ("econ", "econom", "inflation", "rates", "cpi", "gdp", "recession", "earnings", "guidance", "jobs")
    if any(k in theme for k in keys):
        return True
    try:
        for t in themes:
            if any(k in str(t).lower() for k in keys):
                return True
    except Exception:
        pass
    return False


def _compute_bucket(con, bucket_ts_ms: int, bucket_sec: int) -> Tuple[int, float, float, float, float]:
    rows = con.execute(
        """
        SELECT meta_json
        FROM events
        WHERE source='gdelt'
          AND ts_ms >= ?
          AND ts_ms < ?
        """,
        (int(bucket_ts_ms), int(bucket_ts_ms + bucket_sec * 1000)),
    ).fetchall()

    if not rows:
        return 0, 0.0, 0.0, 0.0, 0.0

    tones: List[float] = []
    conflict_n = 0
    econ_n = 0
    doc_n = 0

    for (mj,) in rows:
        doc_n += 1
        try:
            meta = json.loads(mj) if mj else {}
        except Exception:
            meta = {}
        gd = (meta or {}).get("gdelt") or {}
        try:
            t = float(gd.get("tone", 0.0) or 0.0)
        except Exception:
            t = 0.0
        tones.append(float(t))
        if _is_conflict(meta):
            conflict_n += 1
        if _is_econ(meta):
            econ_n += 1

    tone_mean = float(statistics.mean(tones)) if tones else 0.0
    tone_std = float(statistics.pstdev(tones)) if len(tones) >= 2 else 0.0
    conflict_share = float(conflict_n) / float(max(1, doc_n))
    econ_share = float(econ_n) / float(max(1, doc_n))
    return int(doc_n), tone_mean, tone_std, conflict_share, econ_share


def main() -> None:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0
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
                    now_s = time.time()
                    if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
                        touch_job_lock(JOB_NAME, OWNER, PID)
                        put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"bucket_ts_ms": int(b)}))
                        last_hb_s = now_s

                    doc_n, tone_mean, tone_std, conflict_share, econ_share = _compute_bucket(con, int(b), int(BUCKET_SEC))
                    con.execute(
                        """
                        INSERT OR REPLACE INTO gdelt_macro_features(
                          bucket_ts_ms, bucket_sec,
                          doc_count, tone_mean, tone_std,
                          conflict_share, econ_share
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(b),
                            int(BUCKET_SEC),
                            int(doc_n),
                            float(tone_mean),
                            float(tone_std),
                            float(conflict_share),
                            float(econ_share),
                        ),
                    )
                    wrote += 1
                    b += int(BUCKET_SEC) * 1000

                try:
                    con.commit()
                except Exception:
                    pass

                logging.info("buckets_upserted=%s", wrote)
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
