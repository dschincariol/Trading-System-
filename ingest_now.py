# ingest_now.py
import os
import json
import time
import logging
from pathlib import Path

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)

from dev_core.ingest.rss_ingest import ingest_rss_sources
from dev_core.ingest.gdelt_ingest import ingest_gdelt_doc

JOB_NAME = "ingest_now"
OWNER = os.environ.get("JOB_OWNER", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")))
PID = os.getpid()

MAX_ITEMS_PER_SOURCE = int(os.environ.get("RSS_MAX_ITEMS_PER_SOURCE", "15"))
LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "10.0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [ingest_now] %(message)s",
)


def main() -> None:
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, stale_after_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0
    started_ms = int(time.time() * 1000)

    try:
        cfg_path = Path(os.environ.get("RSS_SOURCES_FILE", "sources_rss.json"))
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

        now_s = time.time()
        if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
            touch_job_lock(JOB_NAME, OWNER, PID)
            put_job_heartbeat(JOB_NAME, OWNER, PID)
            put_job_heartbeat(
                JOB_NAME,
                OWNER,
                PID,
                extra_json=json.dumps(
                    {
                        "sources_file": str(cfg_path),
                        "max_items_per_source": MAX_ITEMS_PER_SOURCE,
                    }
                ),
            )
            last_hb_s = now_s

        items, errors = ingest_rss_sources(cfg.get("sources", []), max_items_per_source=MAX_ITEMS_PER_SOURCE)

        # GDELT structured news (optional; uses ACTIVE/WATCH symbols)
        try:
            conu = connect()
            try:
                from dev_core.universe import get_active_symbols
                syms = get_active_symbols(conu, limit=int(os.environ.get("GDELT_SYMBOL_LIMIT", "60")))
            finally:
                conu.close()

            gd_items, gd_errors = ingest_gdelt_doc(
                symbols=syms,
                lookback_minutes=int(os.environ.get("GDELT_LOOKBACK_MINUTES", "45")),
                maxrecords=int(os.environ.get("GDELT_MAXRECORDS", "250")),
            )
            items.extend(gd_items or [])
            errors.extend(gd_errors or [])
        except Exception:
            pass

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
        logging.info("fetched_items=%s upsert_attempts=%s source_errors=%s dur_ms=%s", len(items), upsert_attempts, len(errors), dur_ms)

        if errors:
            for e in errors[:10]:
                logging.warning("rss_error=%s", e)

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
