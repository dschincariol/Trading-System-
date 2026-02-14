"""
jobs/backfill_labels_price_from_prices.py

Build price-derived labels for (symbol, horizon_s, ts_pred_ms).

We sample:
- entry_price = nearest price at/after ts_pred_ms
- exit_price  = nearest price at/after ts_pred_ms + horizon_s*1000

Write:
- labels_price (ret, ret_z, dir)

ret_z is a rolling z-score of returns for that symbol/horizon over a lookback window.
"""

import os
import time
import json
import math
import logging
from typing import Optional, Tuple, List

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)

JOB_NAME = "backfill_labels_price"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [labels_price] %(message)s",
)

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

HORIZONS = [300, 3600]
# how far back to backfill from "now" (days)
LOOKBACK_DAYS = int(os.environ.get("LABELS_PRICE_LOOKBACK_DAYS", "30"))
# rolling window size for zscore
ZSCORE_LOOKBACK = int(os.environ.get("LABELS_PRICE_ZLOOKBACK", "500"))


def _nearest_price_at_or_after(con, symbol: str, ts_ms: int) -> Optional[Tuple[int, float]]:
    row = con.execute(
        """
        SELECT ts_ms, price
        FROM prices
        WHERE symbol=?
          AND ts_ms >= ?
        ORDER BY ts_ms ASC
        LIMIT 1
        """,
        (symbol, int(ts_ms)),
    ).fetchone()
    if not row:
        return None
    try:
        return int(row[0]), float(row[1])
    except Exception:
        return None


def _rolling_z(con, symbol: str, horizon_s: int, new_ret: float) -> float:
    rows = con.execute(
        """
        SELECT ret
        FROM labels_price
        WHERE symbol=?
          AND horizon_s=?
        ORDER BY ts_pred_ms DESC
        LIMIT ?
        """,
        (symbol, int(horizon_s), int(ZSCORE_LOOKBACK)),
    ).fetchall()

    rets: List[float] = []
    for (r,) in rows:
        try:
            rets.append(float(r))
        except Exception:
            pass

    rets.append(float(new_ret))
    if len(rets) < 20:
        return 0.0

    m = sum(rets) / len(rets)
    v = sum((x - m) ** 2 for x in rets) / max(1, (len(rets) - 1))
    sd = math.sqrt(max(1e-12, v))
    z = (float(new_ret) - m) / sd
    if z != z:
        return 0.0
    return float(max(-8.0, min(8.0, z)))


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance holds lock; exiting")
        raise SystemExit(2)

    last_hb_s = 0.0
    since_ms = int((time.time() - LOOKBACK_DAYS * 86400) * 1000)

    # crash-safe resume: skip already-labeled timestamps
    try:
        r = connect().execute(
            "SELECT MAX(ts_pred_ms) FROM labels_price"
        ).fetchone()
        if r and r[0]:
            since_ms = max(since_ms, int(r[0]))
    except Exception:
        pass


    try:
        con = connect()
        try:
            # Use decision timestamps as anchors
            # Assumes decisions has ts_ms, symbol.
            rows = con.execute(
                """
                SELECT ts_ms, symbol
                FROM decisions
                WHERE ts_ms >= ?
                GROUP BY symbol, ts_ms
                ORDER BY ts_ms ASC
                """,
                (int(since_ms),),
            ).fetchall()

            if not rows:
                logging.info("no decisions found in lookback window")
                return

            wrote = 0
            scanned = 0

            for ts_pred_ms, sym in rows:
                scanned += 1
                now_s = time.time()
                if now_s - last_hb_s >= HEARTBEAT_EVERY_S:
                    try:
                        touch_job_lock(JOB_NAME, OWNER, PID)
                        put_job_heartbeat(JOB_NAME, OWNER, PID, extra_json=json.dumps({"symbol": sym, "ts": int(ts_pred_ms)}))
                    except Exception:
                        pass
                    last_hb_s = now_s

                ts_pred_ms = int(ts_pred_ms)
                sym = str(sym)

                # entry
                entry = _nearest_price_at_or_after(con, sym, ts_pred_ms)
                if not entry:
                    continue
                entry_ts, entry_px = entry
                if entry_px <= 0:
                    continue

                for h in HORIZONS:
                    ts_eval_target = ts_pred_ms + int(h) * 1000
                    exitp = _nearest_price_at_or_after(con, sym, ts_eval_target)
                    if not exitp:
                        continue
                    exit_ts, exit_px = exitp
                    if exit_px <= 0:
                        continue

                    ret = (exit_px - entry_px) / entry_px
                    dir_ = 1 if ret > 0 else (-1 if ret < 0 else 0)
                    ret_z = _rolling_z(con, sym, int(h), float(ret))

                    try:
                        con.execute(
                            """
                            INSERT OR REPLACE INTO labels_price(
                              ts_pred_ms, ts_eval_ms, symbol, horizon_s,
                              entry_price, exit_price, ret, ret_z, dir, meta_json
                            ) VALUES (?,?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                int(ts_pred_ms),
                                int(exit_ts),
                                sym,
                                int(h),
                                float(entry_px),
                                float(exit_px),
                                float(ret),
                                float(ret_z),
                                int(dir_),
                                json.dumps(
                                    {"entry_ts_ms": int(entry_ts)},
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                        wrote += 1
                    except Exception:
                        pass

                    except Exception:
                        pass

            con.commit()
            logging.info("done scanned=%s wrote=%s", scanned, wrote)

        finally:
            con.close()

    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
