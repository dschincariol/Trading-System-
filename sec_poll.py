import os
import time
import json
import logging

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)

from dev_core.universe import get_active_symbols
from dev_core.sec.edgar_live import fetch_recent_filings

JOB_NAME = "poll_sec_filings"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [sec_poll] %(message)s",
)

FORMS_ALLOW = set(
    f.strip().upper()
    for f in os.environ.get("SEC_FORMS_ALLOW", "8-K,10-Q,10-K,6-K,S-1,424B2,424B3,13D,13G,4").split(",")
    if f.strip()
)

SYMBOL_LIMIT = int(os.environ.get("SEC_SYMBOL_LIMIT", "250"))
PER_SYMBOL_LIMIT = int(os.environ.get("SEC_PER_SYMBOL_LIMIT", "25"))


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    ts_ms = int(time.time() * 1000)

    con = connect()
    try:
        syms = get_active_symbols(con, limit=SYMBOL_LIMIT)
    finally:
        con.close()

    conw = connect()
    try:
        upserts = 0
        for sym in syms:
            try:
                filings = fetch_recent_filings(sym, limit=PER_SYMBOL_LIMIT)
            except Exception:
                continue

            if not filings:
                continue

            rows = []
            for f in filings:
                form = str(f.get("form") or "").upper()
                if FORMS_ALLOW and form and (form not in FORMS_ALLOW):
                    continue

                rows.append(
                    (
                        sym,
                        f.get("accession"),
                        form,
                        f.get("filed_date"),
                        f.get("report_date"),
                        f.get("cik"),
                        f.get("company_name"),
                        f.get("primary_doc_url"),
                        None,          # items_json (optional future)
                        "sec",
                        ts_ms,
                    )
                )

            if not rows:
                continue

            conw.executemany(
                """
                INSERT OR REPLACE INTO sec_filings(
                  symbol, accession, form, filed_date, report_date,
                  cik, company_name, primary_doc_url,
                  items_json, source, ts_ms
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            upserts += len(rows)

        conw.commit()
        logging.info("sec filings poll complete upserts=%s", upserts)
    finally:
        conw.close()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
