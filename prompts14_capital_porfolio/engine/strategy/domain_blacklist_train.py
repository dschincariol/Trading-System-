import os
import time
import logging
from typing import Dict, Tuple

from engine.storage import connect, init_db, acquire_job_lock, release_job_lock
from engine.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot

JOB_NAME = "train_domain_blacklist"
OWNER = os.environ.get("JOB_OWNER", "system")
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "300"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [domain_blacklist_train] %(message)s",
)

def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    try:
        # make sure ledger is up to date
        try:
            upsert_from_latest_pnl_attribution_snapshot()
        except Exception:
            pass

        con = connect()
        try:
            now_ms = int(time.time() * 1000)
            lookback_days = int(os.environ.get("DOMAIN_PERF_LOOKBACK_DAYS", "90"))
            cutoff_ms = now_ms - lookback_days * 24 * 3600 * 1000

            # Gather pnl by domain/symbol/regime/horizon
            rows = con.execute(
                """
                SELECT
                  t.symbol,
                  COALESCE(UPPER(json_extract(t.regime_vector_json, '$.regime')), 'MID') AS regime,
                  COALESCE(json_extract(t.signal_json, '$.horizon_s'), 0) AS horizon_s,
                  COALESCE(json_extract(a.explain_json, '$.event_meta.domain'), '') AS domain,
                  t.pnl
                FROM trade_attribution_ledger t
                LEFT JOIN alerts a ON a.id = t.source_alert_id
                WHERE t.ts_ms >= ?
                """,
                (int(cutoff_ms),),
            ).fetchall()

            stats: Dict[Tuple[str, str, str, int], Dict[str, float]] = {}
            for sym, reg, h, domain, pnl in rows or []:
                d = str(domain or "").lower().strip()
                s = str(sym or "").upper().strip()
                r = str(reg or "MID").upper().strip()
                h = int(h or 0)
                if not d or not s or h <= 0:
                    continue
                key = (d, s, r, h)
                rec = stats.setdefault(key, {"n": 0.0, "win": 0.0, "sum": 0.0})
                rec["n"] += 1.0
                try:
                    if float(pnl or 0.0) > 0.0:
                        rec["win"] += 1.0
                    rec["sum"] += float(pnl or 0.0)
                except Exception:
                    pass

            # write out aggregated stats and update blacklist
            min_n = int(os.environ.get("DOMAIN_BLACKLIST_MIN_N", "30"))
            for (domain, sym, reg, h), rec in stats.items():
                n = int(rec.get("n", 0))
                win_rate = float(rec.get("win", 0.0) / n) if n else 0.0
                mean_edge = float(rec.get("sum", 0.0) / n) if n else 0.0

                con.execute(
                    """
                    INSERT OR REPLACE INTO domain_perf(
                      ts_ms, domain, symbol, regime, horizon_s,
                      n, win_rate, mean_edge, mean_pnl
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (now_ms, domain, sym, reg, h, n, win_rate, mean_edge, mean_edge),
                )

                status = "ALLOW"
                if n >= min_n and (mean_edge < 0.0 or win_rate < 0.45):
                    status = "BLOCK"
                con.execute(
                    """
                    INSERT OR REPLACE INTO domain_blacklist(
                      domain, symbol, status, score, reason, updated_ts_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (domain, sym, status, mean_edge, "auto", now_ms),
                )

            con.commit()
            logging.info("domain_perf rows=%s", len(stats))
        finally:
            try:
                con.close()
            except Exception:
                pass
    finally:
        try:
            release_job_lock(JOB_NAME, OWNER, PID)
        except Exception:
            pass

if __name__ == "__main__":
    main()
