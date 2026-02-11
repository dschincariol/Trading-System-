"""
update_universe.py

Event-driven dynamic universe maintenance.

Strategy (safe defaults):
- Scan recent events
- Extract ticker-like tokens from title/body
- Upsert into symbols table with score boosts for:
  - recency
  - novelty (if present in events.meta_json)
  - repeated mentions

Then:
- Promote top N to ACTIVE
- Keep a larger set as WATCH
- Decay stale symbols slowly
"""

import json
import os
import time
import logging
from typing import Dict

from dev_core.storage import connect, init_db, acquire_job_lock, release_job_lock, touch_job_lock, put_job_heartbeat
from dev_core.universe import extract_symbol_candidates, upsert_symbol

JOB_NAME = "update_universe"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [update_universe] %(message)s",
)

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))
HEARTBEAT_EVERY_S = float(os.environ.get("HEARTBEAT_EVERY_S", "15.0"))

# How far back to scan for candidate symbols
UNIVERSE_EVENT_LOOKBACK_S = int(os.environ.get("UNIVERSE_EVENT_LOOKBACK_S", "21600"))  # 6h

# Universe sizes (wide by default; tune later)
UNIVERSE_ACTIVE_N = int(os.environ.get("UNIVERSE_ACTIVE_N", "250"))
UNIVERSE_WATCH_N = int(os.environ.get("UNIVERSE_WATCH_N", "2000"))

# --------------------------------------------------
# Social-driven universe boosts (opt-in)
# --------------------------------------------------
UNIVERSE_USE_SOCIAL = os.environ.get("UNIVERSE_USE_SOCIAL", "0") == "1"
UNIVERSE_SOCIAL_LOOKBACK_S = int(os.environ.get("UNIVERSE_SOCIAL_LOOKBACK_S", "21600"))  # 6h
UNIVERSE_SOCIAL_BUCKET_SEC = int(os.environ.get("UNIVERSE_SOCIAL_BUCKET_SEC", "300"))    # 5m
UNIVERSE_SOCIAL_Z_TH = float(os.environ.get("UNIVERSE_SOCIAL_Z_TH", "2.0"))
UNIVERSE_SOCIAL_MIN_AUTHORS = int(os.environ.get("UNIVERSE_SOCIAL_MIN_AUTHORS", "10"))
UNIVERSE_SOCIAL_MAX_MANIP_RISK = float(os.environ.get("UNIVERSE_SOCIAL_MAX_MANIP_RISK", "0.80"))
UNIVERSE_SOCIAL_SCORE_BOOST = float(os.environ.get("UNIVERSE_SOCIAL_SCORE_BOOST", "0.25"))
# --------------------------------------------------
# Baseline liquid universe (seed, WATCH only)
# --------------------------------------------------
BASELINE_SYMBOLS = [
    # Index / macro ETFs
    "SPY","QQQ","IWM","DIA","VTI","VOO","TLT","IEF","SHY",
    # Sector ETFs
    "XLF","XLE","XLK","XLV","XLY","XLP","XLI","XLB","XLU","XLC",
    # Commodities (proxies)
    "GLD","SLV","USO","UNG","DBC",
    # Crypto majors
    "BTC","ETH","SOL","BNB","XRP",
]

def _seed_baseline(con):
    now_ms = int(time.time() * 1000)
    for s in BASELINE_SYMBOLS:
        try:
            con.execute(
                """
                INSERT OR IGNORE INTO symbols(
                  symbol, asset_class, status, score,
                  created_ts_ms, updated_ts_ms
                )
                VALUES (?, ?, 'WATCH', 0.5, ?, ?)
                """,
                (s, "UNKNOWN", now_ms, now_ms),
            )
        except Exception:
            pass

# Decay control
SCORE_DECAY_PER_RUN = float(os.environ.get("UNIVERSE_SCORE_DECAY_PER_RUN", "0.02"))  # 2% per run
MIN_SCORE_FLOOR = float(os.environ.get("UNIVERSE_MIN_SCORE_FLOOR", "-5.0"))

def _now_ms() -> int:
    return int(time.time() * 1000)

def _safe_load_meta(meta_json: str) -> Dict:
    try:
        d = json.loads(meta_json) if meta_json else {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        logging.error("another instance is holding the job lock; exiting")
        raise SystemExit(2)

    started_ms = _now_ms()
    last_hb_s = 0.0

    con = connect()
    try:
        cutoff_ms = _now_ms() - int(UNIVERSE_EVENT_LOOKBACK_S) * 1000
        # Ensure baseline universe exists
        _seed_baseline(con)

        rows = con.execute(
            """
            SELECT id, ts_ms, title, body, meta_json
            FROM events
            WHERE ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT 2000
            """,
            (int(cutoff_ms),),
        ).fetchall()

        if not rows:
            logging.info("no recent events; universe unchanged")
            return

        # small decay each run so stale symbols fall out naturally
        try:
            con.execute(
                """
                UPDATE symbols
                SET score = MAX(?, score * (1.0 - ?)),
                    updated_ts_ms = ?
                """,
                (float(MIN_SCORE_FLOOR), float(SCORE_DECAY_PER_RUN), _now_ms()),
            )
        except Exception:
            pass

        # ingest candidates from events
        for (eid, ts_ms, title, body, meta_json) in rows:
            now_s = time.time()
            if (now_s - last_hb_s) >= HEARTBEAT_EVERY_S:
                try:
                    touch_job_lock(JOB_NAME, OWNER, PID)
                    put_job_heartbeat(
                        JOB_NAME,
                        OWNER,
                        PID,
                        extra_json=json.dumps({"event_id": int(eid), "event_ts_ms": int(ts_ms)}),
                    )
                except Exception:
                    pass
                last_hb_s = now_s

            meta = _safe_load_meta(meta_json)
            novelty = 0.0
            try:
                novelty = float(meta.get("novelty") or 0.0)
                if novelty != novelty:
                    novelty = 0.0
                novelty = max(0.0, min(1.0, novelty))
            except Exception:
                novelty = 0.0

            text = f"{title or ''}\n{body or ''}"
            syms = extract_symbol_candidates(text)
            if not syms:
                continue

            # Score boosts:
            # - recency: newer events implicitly dominate because we iterate newest->oldest
            # - novelty: boosts rare/new events
            base_boost = 0.15 + 0.35 * novelty

            for s in syms:
                upsert_symbol(
                    con,
                    s,
                    status="WATCH",
                    score_delta=float(base_boost),
                    last_seen_event_ts_ms=int(ts_ms),
                    meta={"last_event_id": int(eid)},
                )

        # --------------------------------------------------
        # Optional: ingest candidates from SOCIAL (attention spikes)
        # --------------------------------------------------
        if UNIVERSE_USE_SOCIAL:
            try:
                social_cutoff_ms = _now_ms() - int(UNIVERSE_SOCIAL_LOOKBACK_S) * 1000
                rows_s = con.execute(
                    """
                    SELECT symbol,
                           MAX(mention_rate_z) AS max_z,
                           MAX(unique_authors) AS max_u,
                           MIN(manip_risk) AS min_m
                    FROM social_features
                    WHERE bucket_sec = ?
                      AND bucket_ts_ms >= ?
                    GROUP BY symbol
                    ORDER BY max_z DESC
                    LIMIT 2000
                    """,
                    (int(UNIVERSE_SOCIAL_BUCKET_SEC), int(social_cutoff_ms)),
                ).fetchall()

                for (sym, max_z, max_u, min_m) in rows_s or []:
                    try:
                        if float(max_z or 0.0) < float(UNIVERSE_SOCIAL_Z_TH):
                            continue
                        if int(max_u or 0) < int(UNIVERSE_SOCIAL_MIN_AUTHORS):
                            continue
                        if float(min_m or 0.0) >= float(UNIVERSE_SOCIAL_MAX_MANIP_RISK):
                            continue
                    except Exception:
                        continue

                    upsert_symbol(
                        con,
                        str(sym),
                        status="WATCH",
                        score_delta=float(UNIVERSE_SOCIAL_SCORE_BOOST),
                        last_seen_event_ts_ms=None,
                        meta={"social_max_z": float(max_z or 0.0), "social_max_u": int(max_u or 0)},
                    )
            except Exception:
                pass

        # --------------------------------------------------
        # HARD TRADABILITY FILTERS (disable garbage)
        # --------------------------------------------------
        now_ms = _now_ms()
        rows2 = con.execute(
            """
            SELECT symbol, meta_json
            FROM symbols
            """
        ).fetchall()

        for sym, meta_json in rows2:
            try:
                meta = json.loads(meta_json) if meta_json else {}
            except Exception:
                meta = {}

            trad = meta.get("tradability") if isinstance(meta, dict) else {}
            last_px_ts = trad.get("last_price_ts_ms")
            vol_proxy = trad.get("vol_proxy", 0.0)

            # No recent prices → DISABLED
            if not last_px_ts or (now_ms - int(last_px_ts)) > 15 * 60 * 1000:
                con.execute(
                    "UPDATE symbols SET status='DISABLED', updated_ts_ms=? WHERE symbol=?",
                    (now_ms, sym),
                )
                continue

            # Completely dead volatility → COOLDOWN
            try:
                if float(vol_proxy) < 1e-4:
                    con.execute(
                        "UPDATE symbols SET status='COOLDOWN', updated_ts_ms=? WHERE symbol=?",
                        (now_ms, sym),
                    )
            except Exception:
                pass

        # Promote top N to ACTIVE, keep next set as WATCH
        # Everyone else stays in their current state unless decayed hard by score.
        top = con.execute(
            """
            SELECT symbol
            FROM symbols
            WHERE status != 'DISABLED'
            ORDER BY score DESC, updated_ts_ms DESC
            LIMIT ?
            """,
            (int(max(UNIVERSE_ACTIVE_N, UNIVERSE_WATCH_N)),),
        ).fetchall()

        top_syms = [str(r[0]) for r in top or []]
        active_set = set(top_syms[: int(UNIVERSE_ACTIVE_N)])
        watch_set = set(top_syms[: int(UNIVERSE_WATCH_N)])

        # set ACTIVE
        if active_set:
            con.execute(
                f"UPDATE symbols SET status='ACTIVE', updated_ts_ms=? WHERE symbol IN ({','.join('?' for _ in sorted(active_set))})",
                (_now_ms(), *sorted(active_set)),
            )

        # set WATCH (but don't override ACTIVE)
        watch_only = sorted([s for s in watch_set if s not in active_set])
        if watch_only:
            con.execute(
                f"UPDATE symbols SET status='WATCH', updated_ts_ms=? WHERE symbol IN ({','.join('?' for _ in watch_only)})",
                (_now_ms(), *watch_only),
            )

        con.commit()
        dur_ms = _now_ms() - started_ms
        logging.info("universe updated: active=%s watch=%s dur_ms=%s", len(active_set), len(watch_set), dur_ms)

    finally:
        con.close()
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass

if __name__ == "__main__":
    main()
