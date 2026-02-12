"""
Execution Analytics & Slippage Attribution Engine

Post-trade diagnostics:
- Realized slippage vs decision ref price
- Alpha decay at fill
- TTL breach detection
- Cancel/replace impact
- Aggressiveness attribution
- Broker performance stats
"""

import json
import math
import time
from typing import Dict, Any, List, Optional

from dev_core.storage import connect


# ============================================================
# Helpers
# ============================================================

def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_analytics (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          client_order_id TEXT NOT NULL,
          broker TEXT,
          symbol TEXT,
          submit_ts_ms INTEGER,
          fill_ts_ms INTEGER,
          decision_ref_px REAL,
          fill_px REAL,
          qty REAL,
          slippage_bps REAL,
          alpha_remaining_at_fill REAL,
          ttl_ms INTEGER,
          age_ms INTEGER,
          aggressiveness TEXT,
          order_type TEXT,
          created_ts_ms INTEGER NOT NULL,
          meta_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_exec_analytics_cid
        ON execution_analytics(client_order_id);

        CREATE INDEX IF NOT EXISTS idx_exec_analytics_sym
        ON execution_analytics(symbol);
        """
    )


def _alpha_remaining(age_ms: int, half_life_ms: int, ttl_ms: int) -> float:
    if ttl_ms <= 0:
        return 0.0
    if age_ms >= ttl_ms:
        return 0.0
    hl = max(1, half_life_ms)
    decay = math.pow(0.5, float(age_ms) / float(hl))
    ttl_factor = max(0.0, 1.0 - (float(age_ms) / float(ttl_ms)))
    return max(0.0, min(1.0, decay * ttl_factor))

def summarize_execution_performance(days: int = 7) -> Dict[str, Any]:

    con = connect()
    try:
        since = _now_ms() - (int(days) * 86400000)

        rows = con.execute(
            """
            SELECT
              broker,
              symbol,
              COUNT(*),
              AVG(slippage_bps),
              AVG(alpha_remaining_at_fill),
              AVG(age_ms)
            FROM execution_analytics
            WHERE fill_ts_ms >= ?
            GROUP BY broker, symbol
            """,
            (int(since),),
        ).fetchall()

        out = []

        for r in rows or []:
            broker, symbol, n, avg_slip, avg_alpha, avg_age = r
            out.append(
                {
                    "broker": broker,
                    "symbol": symbol,
                    "fills": int(n),
                    "avg_slippage_bps": float(avg_slip or 0.0),
                    "avg_alpha_remaining": float(avg_alpha or 0.0),
                    "avg_fill_latency_ms": float(avg_age or 0.0),
                }
            )

        return {"ok": True, "summary": out}

    finally:
        con.close()

# ============================================================
# Core Analytics Builder
# ============================================================

def build_execution_analytics(limit: int = 5000) -> Dict[str, Any]:

    con = connect()
    try:
        _ensure_tables(con)

        rows = con.execute(
            """
            SELECT
              s.client_order_id,
              s.broker,
              s.symbol,
              s.qty,
              s.submit_ts_ms,
              s.ref_px,
              f.fill_ts_ms,
              f.fill_px,
              s.extra_json
            FROM execution_submits s
            JOIN execution_fills f
              ON s.client_order_id = f.client_order_id
            ORDER BY f.fill_ts_ms DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()

        n = 0

        for r in rows or []:
            (
                cid,
                broker,
                symbol,
                qty,
                submit_ts_ms,
                ref_px,
                fill_ts_ms,
                fill_px,
                extra_json,
            ) = r

            try:
                qty = float(qty or 0.0)
                ref_px = float(ref_px or 0.0)
                fill_px = float(fill_px or 0.0)
                submit_ts_ms = int(submit_ts_ms or 0)
                fill_ts_ms = int(fill_ts_ms or 0)
            except Exception:
                continue

            if qty == 0 or ref_px <= 0 or fill_px <= 0:
                continue

            side_sign = 1.0 if qty > 0 else -1.0
            slippage_bps = ((fill_px - ref_px) / ref_px) * 10000.0 * side_sign

            age_ms = max(0, fill_ts_ms - submit_ts_ms)

            extra = {}
            try:
                extra = json.loads(extra_json or "{}")
            except Exception:
                extra = {}

            ttl_ms = int(extra.get("alpha_ttl_ms") or 0)
            half_life_ms = int(extra.get("alpha_half_life_ms") or 60000)

            alpha_rem = _alpha_remaining(age_ms, half_life_ms, ttl_ms)

            aggressiveness = str(extra.get("aggressiveness") or "")
            order_type = str(extra.get("order_type") or "")

            con.execute(
                """
                INSERT INTO execution_analytics(
                  client_order_id,
                  broker,
                  symbol,
                  submit_ts_ms,
                  fill_ts_ms,
                  decision_ref_px,
                  fill_px,
                  qty,
                  slippage_bps,
                  alpha_remaining_at_fill,
                  ttl_ms,
                  age_ms,
                  aggressiveness,
                  order_type,
                  created_ts_ms,
                  meta_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(cid),
                    str(broker),
                    str(symbol),
                    int(submit_ts_ms),
                    int(fill_ts_ms),
                    float(ref_px),
                    float(fill_px),
                    float(qty),
                    float(slippage_bps),
                    float(alpha_rem),
                    int(ttl_ms),
                    int(age_ms),
                    aggressiveness,
                    order_type,
                    _now_ms(),
                    json.dumps(extra, separators=(",", ":"), sort_keys=True),
                ),
            )

            n += 1

        con.commit()
        return {"ok": True, "records_written": int(n)}

    finally:
        con.close()
