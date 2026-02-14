"""
Cross-provider routing + anomaly detection + health scoring
"""

import os
import time
from typing import Dict, Optional

from engine.dev_core.storage import connect

ANOMALY_THRESHOLD_BPS = float(os.environ.get("PROVIDER_ANOMALY_BPS", "25"))
STALE_THRESHOLD_MS = int(os.environ.get("PROVIDER_STALE_MS", "2000"))

def now_ms():
    return int(time.time() * 1000)


def _bps(a: float, b: float) -> float:
    if not a or not b:
        return 0.0
    mid = (a + b) / 2.0
    return abs(a - b) / mid * 10000.0 if mid else 0.0


def detect_cross_provider_anomalies():
    ts = now_ms()
    con = connect(readonly=False)

    try:
        rows = con.execute(
            """
            SELECT symbol, provider, last, bid, ask, ts_ms
            FROM price_quotes_raw
            WHERE ts_ms > ?
            """,
            (ts - 5000,),
        ).fetchall()

        by_symbol: Dict[str, Dict[str, Dict]] = {}

        for sym, provider, last, bid, ask, pts in rows:
            by_symbol.setdefault(sym, {})[provider] = {
                "last": last,
                "bid": bid,
                "ask": ask,
                "ts_ms": pts,
            }

        for sym, providers in by_symbol.items():
            if len(providers) < 2:
                continue

            keys = list(providers.keys())
            a, b = keys[0], keys[1]
            pa, pb = providers[a], providers[b]

            if not pa.get("last") or not pb.get("last"):
                continue

            spread_bps = _bps(pa["last"], pb["last"])

            if spread_bps > ANOMALY_THRESHOLD_BPS:
                con.execute(
                    """
                    INSERT OR REPLACE INTO price_anomalies
                    (ts_ms, symbol, provider_a, provider_b, spread_diff, reason)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        ts,
                        sym,
                        a,
                        b,
                        spread_bps,
                        "cross_provider_spread",
                    ),
                )

        con.commit()

    finally:
        con.close()


def compute_provider_health():
    ts = now_ms()
    con = connect(readonly=False)

    try:
        rows = con.execute(
            """
            SELECT provider, MAX(ts_ms)
            FROM price_quotes_raw
            GROUP BY provider
            """
        ).fetchall()

        for provider, last_ts in rows:
            stale = ts - int(last_ts or 0)
            latency = stale

            score = 1.0
            status = "OK"

            if stale > STALE_THRESHOLD_MS:
                score -= 0.5
                status = "STALE"

            con.execute(
                """
                INSERT OR REPLACE INTO provider_health
                (ts_ms, provider, latency_ms, stale_ms, score, status)
                VALUES (?,?,?,?,?,?)
                """,
                (
                    ts,
                    provider,
                    latency,
                    stale,
                    score,
                    status,
                ),
            )

        con.commit()
    finally:
        con.close()


def route_best_price(symbol: str) -> Optional[float]:
    con = connect(readonly=True)
    try:
        rows = con.execute(
            """
            SELECT r.provider, r.last, h.score
            FROM price_quotes_raw r
            LEFT JOIN provider_health h
            ON r.provider = h.provider
            WHERE r.symbol = ?
            ORDER BY h.score DESC
            LIMIT 2
            """,
            (symbol,),
        ).fetchall()

        if not rows:
            return None

        if len(rows) == 1:
            return rows[0][1]

        p1, p2 = rows[0], rows[1]
        if not p1[1] or not p2[1]:
            return p1[1] or p2[1]

        return (p1[1] * p1[2] + p2[1] * p2[2]) / (p1[2] + p2[2])

    finally:
        con.close()
