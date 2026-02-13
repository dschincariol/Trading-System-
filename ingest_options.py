import os
import json
import time
import logging

from dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

from dev_core.broker_fill_utils import get_realized_trade
from dev_core.alpha_lifecycle_engine import compute_alpha_decay_metrics
_REGION_MAP_CACHE = None

from dev_core.ingest.options_polygon import fetch_options_chain_snapshot


JOB_NAME = "ingest_options"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "10.0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Options snapshot controls
OPT_LIMIT = int(os.environ.get("OPTIONS_SNAPSHOT_LIMIT", "250"))
OPT_MAX_PAGES = int(os.environ.get("OPTIONS_SNAPSHOT_MAX_PAGES", "4"))
OPT_TIMEOUT_S = int(os.environ.get("OPTIONS_SNAPSHOT_TIMEOUT_S", "8"))
OPT_SYMBOL_LIMIT = int(os.environ.get("OPTIONS_UNDERLYING_LIMIT", "20"))

# Optional filters
OPT_CONTRACT_TYPE = os.environ.get("OPTIONS_CONTRACT_TYPE", "").strip().lower() or None  # "call" | "put"
OPT_EXPIRATION_DATE = os.environ.get("OPTIONS_EXPIRATION_DATE", "").strip() or None      # "YYYY-MM-DD"


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [ingest_options] %(message)s",
)


def _get_underlyings(con, limit: int):
    rows = con.execute(
        """
        SELECT symbol
        FROM symbols
        WHERE status IN ('ACTIVE','WATCH')
        ORDER BY score DESC, updated_ts_ms DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall() or []
    return [str(r[0]) for r in rows if r and r[0]]


def _put_options_rows(con, rows):
    con.executemany(
        """
        INSERT INTO options_chain(
          ts_ms, underlying, contract, expiration, contract_type, strike,
          iv, open_interest, volume, bid, ask,
          delta, gamma, theta, vega,
          source
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(contract, ts_ms) DO UPDATE SET
          underlying=excluded.underlying,
          expiration=excluded.expiration,
          contract_type=excluded.contract_type,
          strike=excluded.strike,
          iv=excluded.iv,
          open_interest=excluded.open_interest,
          volume=excluded.volume,
          bid=excluded.bid,
          ask=excluded.ask,
          delta=excluded.delta,
          gamma=excluded.gamma,
          theta=excluded.theta,
          vega=excluded.vega,
          source=excluded.source
        """,
        [
            (
                int(r["ts_ms"]),
                str(r["underlying"]),
                str(r["contract"]),
                (str(r["expiration"]) if r.get("expiration") is not None else None),
                (str(r["contract_type"]) if r.get("contract_type") is not None else None),
                (float(r["strike"]) if r.get("strike") is not None else None),
                (float(r["iv"]) if r.get("iv") is not None else None),
                (float(r["open_interest"]) if r.get("open_interest") is not None else None),
                (float(r["volume"]) if r.get("volume") is not None else None),
                (float(r["bid"]) if r.get("bid") is not None else None),
                (float(r["ask"]) if r.get("ask") is not None else None),
                (float(r["delta"]) if r.get("delta") is not None else None),
                (float(r["gamma"]) if r.get("gamma") is not None else None),
                (float(r["theta"]) if r.get("theta") is not None else None),
                (float(r["vega"]) if r.get("vega") is not None else None),
                str(r.get("source") or "polygon"),
            )
            for r in (rows or [])
        ],
    )


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    started_ms = int(time.time() * 1000)
    last_hb_s = 0.0

    try:
        now_s = time.time()
        if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
            touch_job_lock(JOB_NAME, OWNER, PID)
            put_job_heartbeat(
                JOB_NAME,
                OWNER,
                PID,
                extra_json=json.dumps(
                    {
                        "limit": OPT_LIMIT,
                        "max_pages": OPT_MAX_PAGES,
                        "timeout_s": OPT_TIMEOUT_S,
                        "underlying_limit": OPT_SYMBOL_LIMIT,
                        "contract_type": OPT_CONTRACT_TYPE,
                        "expiration_date": OPT_EXPIRATION_DATE,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
            last_hb_s = now_s

        con = connect()
        try:
            underlyings = _get_underlyings(con, OPT_SYMBOL_LIMIT)
        finally:
            con.close()

        total_rows = 0
        total_errs = 0

        conw = connect()
        try:
            for u in underlyings:
                rows, err = fetch_options_chain_snapshot(
                    underlying=u,
                    contract_type=OPT_CONTRACT_TYPE,
                    expiration_date=OPT_EXPIRATION_DATE,
                    limit=OPT_LIMIT,
                    max_pages=OPT_MAX_PAGES,
                    timeout_s=OPT_TIMEOUT_S,
                )
                if err:
                    total_errs += 1
                    logging.warning("underlying=%s err=%s", u, err)

                if rows:
                    _put_options_rows(conw, rows)
                    total_rows += len(rows)

            conw.commit()
        finally:
            try:
                conw.close()
            except Exception:
                pass

        dur_ms = int(time.time() * 1000) - started_ms
        logging.info("underlyings=%s rows=%s errs=%s dur_ms=%s", len(underlyings), total_rows, total_errs, dur_ms)

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
