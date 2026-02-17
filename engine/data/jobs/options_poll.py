import time
import os
import logging
from dotenv import load_dotenv
load_dotenv()

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
)

from engine.dev_core.options.tradier_live import fetch_options_chain
from engine.dev_core.options.options_polygon import fetch_options_chain_snapshot
from engine.dev_core.universe import get_active_symbols

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

    provider = os.environ.get("OPTIONS_PROVIDER", "tradier").lower().strip()

    conw = connect()
    try:
        for sym in syms:
            if provider == "polygon":
                contracts, err = fetch_options_chain_snapshot(sym, limit=250, max_pages=4)
                if err:
                    logging.warning("polygon options error %s: %s", sym, err)
                if contracts:
                    conw.executemany(
                        """
                        INSERT OR REPLACE INTO options_chain_v2(
                          ts_ms,
                          underlying, contract, expiration, contract_type, strike,
                          iv, open_interest, volume,
                          bid, ask,
                          delta, gamma, theta, vega,
                          source
                        )
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        [
                            (
                                int(c.get("ts_ms") or ts_ms),
                                str(c.get("underlying") or sym),
                                str(c.get("contract")),
                                (str(c.get("expiration")) if c.get("expiration") is not None else None),
                                (str(c.get("contract_type")) if c.get("contract_type") is not None else None),
                                (float(c.get("strike")) if c.get("strike") is not None else None),
                                (float(c.get("iv")) if c.get("iv") is not None else None),
                                (float(c.get("open_interest")) if c.get("open_interest") is not None else None),
                                (float(c.get("volume")) if c.get("volume") is not None else None),
                                (float(c.get("bid")) if c.get("bid") is not None else None),
                                (float(c.get("ask")) if c.get("ask") is not None else None),
                                (float(c.get("delta")) if c.get("delta") is not None else None),
                                (float(c.get("gamma")) if c.get("gamma") is not None else None),
                                (float(c.get("theta")) if c.get("theta") is not None else None),
                                (float(c.get("vega")) if c.get("vega") is not None else None),
                                str(c.get("source") or "polygon"),
                            )
                            for c in contracts
                            if c.get("contract")
                        ],
                    )
                continue

            # default: Tradier (legacy v1 table)
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
        logging.info("options poll complete provider=%s", provider)
    finally:
        conw.close()
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
