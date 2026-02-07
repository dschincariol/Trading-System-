import os
import time
import json
import logging

from dev_core.storage import (
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    connect,
    put_event,
)

from dev_core.universe import get_active_symbols
from dev_core.ingest.gdelt_ingest import ingest_gdelt_doc

JOB_NAME = "poll_gdelt"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "10.0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Controls
LOOKBACK_MINUTES = int(os.environ.get("GDELT_LOOKBACK_MINUTES", "45"))
MAXRECORDS = int(os.environ.get("GDELT_MAXRECORDS", "250"))
SYMBOL_LIMIT = int(os.environ.get("GDELT_SYMBOL_LIMIT", "60"))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [gdelt_poll] %(message)s",
)


def main() -> None:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0
    started_ms = int(time.time() * 1000)

    try:
        con = connect()
        try:
            syms = get_active_symbols(con, limit=SYMBOL_LIMIT)
        except Exception:
            syms = []
        finally:
            try:
                con.close()
            except Exception:
                pass

        now_s = time.time()
        if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
            touch_job_lock(JOB_NAME, OWNER, PID)
            put_job_heartbeat(
                JOB_NAME,
                OWNER,
                PID,
                extra_json=json.dumps(
                    {
                        "lookback_minutes": LOOKBACK_MINUTES,
                        "maxrecords": MAXRECORDS,
                        "symbol_limit": SYMBOL_LIMIT,
                        "symbols_n": len(syms),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            last_hb_s = now_s

        items, errors = ingest_gdelt_doc(
            symbols=syms,
            lookback_minutes=LOOKBACK_MINUTES,
            maxrecords=MAXRECORDS,
            language=os.environ.get("GDELT_LANGUAGE", "english"),
        )

        upsert_attempts = 0
        for it in items:
            put_event(
                ts_ms=it["ts_ms"],
                source=it["source"],
                title=it["title"],
                body=it["body"],
                url=it["url"],
                event_key=it["event_key"],
                meta_json=it.get("meta_json"),
            )
            upsert_attempts += 1

        dur_ms = int(time.time() * 1000) - started_ms
        logging.info(
            "gdelt_items=%s upsert_attempts=%s errors=%s dur_ms=%s",
            len(items),
            upsert_attempts,
            len(errors),
            dur_ms,
        )

        if errors:
            for e in errors[:10]:
                logging.warning("gdelt_error=%s", e)

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
