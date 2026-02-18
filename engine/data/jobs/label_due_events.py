# label_due_events.py
"""
Creates labels for events where the horizon has passed and price data exists.
Reads from prices table (live polled).

Writes to labels table.
- Stores vol_proxy + regime at label-time (required for true regime-aware training).

Production wrapper:
- Job locking
- Heartbeats
- Crash safety
"""

import os
import time
import json
import logging
import random
from typing import Optional

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
    put_event,
)
from engine.dev_core.model_v2 import classify_regime

# ---------------            -- ------------------------------------------------------
# Job / runtime config
# ---------------            -- ------------------------------------------------------

JOB_NAME = "label_due_events"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [label_due_events] %(message)s",
)

# ---------------            -- ------------------------------------------------------
# Labeling configuration (from old file)
# ---------------            -- ------------------------------------------------------

HORIZONS_S = [300, 3600]  # 5m, 1h
SYMBOLS = ["SPY", "BTC", "OIL"]

# ---------------            -- ------------------------------------------------------
# Helpers (from old file)
# ---------------            -- ------------------------------------------------------

def price_at_or_after(con, symbol: str, ts_ms: int) -> Optional[float]:
    row = con.execute(
        """
        SELECT price
        FROM prices
        WHERE symbol=? AND ts_ms>=?
        ORDER BY ts_ms ASC
        LIMIT 1
        """,
        (symbol, int(ts_ms)),
    ).fetchone()
    return float(row[0]) if row else None


def compute_return(con, symbol: str, event_ts: int, horizon_ms: int) -> Optional[float]:
    p0 = price_at_or_after(con, symbol, event_ts)
    p1 = price_at_or_after(con, symbol, event_ts + horizon_ms)
    if p0 is None or p1 is None:
        return None
    return (p1 - p0) / p0


def realized_vol_proxy(con, symbol: str, lookback_points: int = 50) -> float:
    rows = con.execute(
        """
        SELECT price
        FROM prices
        WHERE symbol=?
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (symbol, int(lookback_points)),
    ).fetchall()

    if len(rows) < 3:
        return 1e-6

    prices = [float(r[0]) for r in rows][::-1]
    rets = []
    for i in range(1, len(prices)):
        p0, p1 = prices[i - 1], prices[i]
        if p0 > 0:
            rets.append((p1 - p0) / p0)

    if not rets:
        return 1e-6

    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / max(1, (len(rets) - 1))
    vol = var ** 0.5
    return max(vol, 1e-6)

# ---------------            -- ------------------------------------------------------
# Core labeling logic (extracted & reusable)
# ---------------            -- ------------------------------------------------------

def label_due_events_internal() -> int:
    con = connect()
    try:
        now_ms = int(time.time() * 1000)
        max_h_ms = max(HORIZONS_S) * 1000

        events = con.execute(
            """
            SELECT id, ts_ms, title
            FROM events
            WHERE ts_ms <= ?
            ORDER BY ts_ms ASC
            LIMIT 200
            """,
            (int(now_ms - max_h_ms),),
        ).fetchall()

        inserted = 0

        for eid, ets, _title in events:
            for sym in SYMBOLS:
                vol = realized_vol_proxy(con, sym, lookback_points=80)
                regime = classify_regime(vol)

                for h_s in HORIZONS_S:
                    exists = con.execute(
                        """
                        SELECT 1
                        FROM labels
                        WHERE event_id=? AND symbol=? AND horizon_s=?
                        LIMIT 1
                        """,
                        (int(eid), sym, int(h_s)),
                    ).fetchone()
                    if exists:
                        continue

                    ret = compute_return(con, sym, int(ets), int(h_s) * 1000)
                    if ret is None:
                        continue

                    impact_z = float(ret) / float(vol)

                    con.execute(
                        """
                        INSERT OR IGNORE INTO labels(
                          event_id, horizon_s, symbol,
                          baseline_ret, realized_ret,
                          impact_z, created_at_ms,
                          vol_proxy, regime
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            int(eid),
                            int(h_s),
                            sym,
                            0.0,
                            float(ret),
                            float(impact_z),
                            int(now_ms),
                            float(vol),
                            str(regime),
                        ),
                    )
                    inserted += 1

        con.commit()
        return inserted
    finally:
        con.close()

# ---------------            -- ------------------------------------------------------
# Runtime helpers
# ---------------            -- ------------------------------------------------------

def _sleep_with_jitter(seconds: float) -> None:
    if seconds <= 0:
        return
    j = seconds * 0.2
    time.sleep(max(0.05, seconds + random.uniform(-j, j)))

# ---------------            -- ------------------------------------------------------
# Main (production runner)
# ---------------            -- ------------------------------------------------------

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    started_ms = int(time.time() * 1000)
    last_hb_s = 0.0

    try:
        logging.info("labeling due events")

        n = label_due_events_internal()

        now_s = time.time()
        if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
            touch_job_lock(JOB_NAME, OWNER, PID)
            put_job_heartbeat(JOB_NAME, OWNER, PID)
            put_job_heartbeat(
                JOB_NAME,
                OWNER,
                PID,
                extra_json=json.dumps({"labeled": int(n)}),
            )
            last_hb_s = now_s

        dur_ms = int(time.time() * 1000) - started_ms
        logging.info("labeled_events=%s dur_ms=%s", int(n), int(dur_ms))

    except Exception as e:
        logging.exception("labeling failed: %r", e)
        _sleep_with_jitter(5.0)
        raise
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass


if __name__ == "__main__":
    main()
