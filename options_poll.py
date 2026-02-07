import time
import os
import logging

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)

from dev_core.options.tradier_live import fetch_options_chain
from dev_core.universe import get_active_symbols

JOB_NAME = "poll_options"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [poll_options] %(message)s",
)

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    ts_ms = int(time.time() * 1000)
    con = connect()
    try:
        syms = get_active_symbols(con, limit=50)  # liquidity-focused
    finally:
        con.close()

    conw = connect()
    try:
        for sym in syms:
            rows = fetch_options_chain(sym)
            if not rows:
                continue

            conw.executemany(
                """
                INSERT INTO options_chain(
                  ts_ms, symbol, expiry, strike, call_put,
                  iv, open_interest, volume, source
                )
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        ts_ms,
                        sym,
                        r["expiry"],
                        r["strike"],
                        r["call_put"],
                        r.get("iv"),
                        r.get("open_interest"),
                        r.get("volume"),
                        "tradier",
                    )
                    for r in rows
                ],
            )

        conw.commit()
        logging.info("options poll complete")
    finally:
        conw.close()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
