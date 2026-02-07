import os
import time
import logging
from datetime import date, timedelta

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)
from dev_core.calendar.fmp_earnings import fetch_earnings_calendar

JOB_NAME = "poll_earnings"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [earnings_poll] %(message)s",
)

LOOKAHEAD_DAYS = int(os.environ.get("EARNINGS_LOOKAHEAD_DAYS", "21"))


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    ts_ms = int(time.time() * 1000)
    d0 = date.today()
    d1 = d0 + timedelta(days=LOOKAHEAD_DAYS)

    from_date = d0.isoformat()
    to_date = d1.isoformat()

    items = []
    try:
        items = fetch_earnings_calendar(from_date, to_date)
    except Exception:
        items = []

    conw = connect()
    try:
        upserts = 0
        for it in items or []:
            try:
                sym = str(it.get("symbol") or "").upper().strip()
                dt = str(it.get("date") or "").strip()  # YYYY-MM-DD
                if not sym or not dt:
                    continue

                tod = str(it.get("time") or "unknown").lower().strip()

                conw.execute(
                    """
                    INSERT OR REPLACE INTO earnings_calendar(
                      symbol, earnings_date, time_of_day,
                      eps_est, eps_act, revenue_est, revenue_act,
                      source, updated_ts_ms
                    )
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sym,
                        dt,
                        tod,
                        it.get("epsEstimated"),
                        it.get("eps"),
                        it.get("revenueEstimated"),
                        it.get("revenue"),
                        "fmp",
                        int(ts_ms),
                    ),
                )
                upserts += 1
            except Exception:
                continue

        conw.commit()
        logging.info("earnings poll complete upserts=%s window=%s..%s", upserts, from_date, to_date)
    finally:
        conw.close()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
