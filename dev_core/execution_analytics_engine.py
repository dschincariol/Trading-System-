# FIND (replace entire file content)
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


def _ensure_tables(con):
    con.executescript(
        """
-- ============================================================
-- Canonical execution analytics table (aligned with code below)
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_analytics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,                 -- primary event timestamp (fill_ts_ms)
  client_order_id TEXT NOT NULL,
  broker TEXT,
  symbol TEXT NOT NULL,

  submit_ts_ms INTEGER,
  fill_ts_ms INTEGER,

  decision_ref_px REAL,                   -- reference/decision price at submit
  fill_px REAL,
  qty REAL,

  slippage_bps REAL,
  fee_bps REAL,
  total_cost_bps REAL,                    -- slippage + fees (best-effort)

  alpha_remaining_at_fill REAL,
  ttl_ms INTEGER,
  age_ms INTEGER,                         -- fill_ts_ms - submit_ts_ms

  aggressiveness TEXT,
  order_type TEXT,

  created_ts_ms INTEGER,                  -- insertion time
  meta_json TEXT                          -- serialized extra/meta blob
);

CREATE INDEX IF NOT EXISTS idx_execution_analytics_ts ON execution_analytics(ts_ms);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_symbol ON execution_analytics(symbol);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_broker ON execution_analytics(broker);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_cid ON execution_analytics(client_order_id);

-- ---------------------------------------------
-- Slippage feedback loop (rolling realized costs)
-- ---------------------------------------------
CREATE TABLE IF NOT EXISTS execution_slippage_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  broker TEXT,
  order_type TEXT,
  aggressiveness TEXT,
  sample_n INTEGER NOT NULL,
  median_slippage_bps REAL,
  p75_slippage_bps REAL,
  suggested_limit_offset_bps REAL,
  suggested_extra_slip_bps REAL,
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_exec_slip_fb_ts ON execution_slippage_feedback(ts_ms);
CREATE INDEX IF NOT EXISTS idx_exec_slip_fb_broker ON execution_slippage_feedback(broker);

-- ---------------------------------------------
-- Alpha preservation KPIs (post-trade diagnostics)
-- ---------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_preservation_kpis (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  broker TEXT,
  symbol TEXT,
  order_type TEXT,
  aggressiveness TEXT,
  sample_n INTEGER NOT NULL,
  avg_alpha_remaining REAL,
  avg_total_cost_bps REAL,
  alpha_cost_efficiency REAL,
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_alpha_kpis_ts ON alpha_preservation_kpis(ts_ms);
CREATE INDEX IF NOT EXISTS idx_alpha_kpis_broker ON alpha_preservation_kpis(broker);
CREATE INDEX IF NOT EXISTS idx_alpha_kpis_symbol ON alpha_preservation_kpis(symbol);
        """
    )


def _alpha_remaining(age_ms: int, half_life_ms: int, ttl_ms: int) -> float:
    if ttl_ms <= 0:
        return 0.0
    if age_ms >= ttl_ms:
        return 0.0
    hl = max(1, int(half_life_ms))
    decay = math.pow(0.5, float(age_ms) / float(hl))
    ttl_factor = max(0.0, 1.0 - (float(age_ms) / float(ttl_ms)))
    return max(0.0, min(1.0, decay * ttl_factor))


def summarize_execution_performance(days: int = 7) -> Dict[str, Any]:
    con = connect()
    try:
        _ensure_tables(con)
        since = _now_ms() - (int(days) * 86400000)

        rows = con.execute(
            """
            SELECT
              broker,
              symbol,
              COUNT(*) AS n,
              AVG(slippage_bps) AS avg_slip,
              AVG(alpha_remaining_at_fill) AS avg_alpha,
              AVG(age_ms) AS avg_age
            FROM execution_analytics
            WHERE ts_ms >= ?
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
                    "fills": int(n or 0),
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

        wrote = 0

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

            if qty == 0.0 or ref_px <= 0.0 or fill_px <= 0.0 or fill_ts_ms <= 0:
                continue

            side_sign = 1.0 if qty > 0 else -1.0
            slippage_bps = ((fill_px - ref_px) / ref_px) * 10000.0 * side_sign

            age_ms = max(0, int(fill_ts_ms) - int(submit_ts_ms))

            extra: Dict[str, Any]
            try:
                extra = json.loads(extra_json or "{}")
                if not isinstance(extra, dict):
                    extra = {}
            except Exception:
                extra = {}

            ttl_ms = int(extra.get("alpha_ttl_ms") or 0)
            half_life_ms = int(extra.get("alpha_half_life_ms") or 60000)

            alpha_rem = _alpha_remaining(age_ms, half_life_ms, ttl_ms)

            aggressiveness = str(extra.get("aggressiveness") or "")
            order_type = str(extra.get("order_type") or "")

            fee_bps = 0.0
            try:
                fee_bps = float(extra.get("fee_bps") or 0.0)
            except Exception:
                fee_bps = 0.0

            total_cost_bps = float(slippage_bps) + float(fee_bps)

            con.execute(
                """
                INSERT INTO execution_analytics(
                  ts_ms,
                  client_order_id,
                  broker,
                  symbol,
                  submit_ts_ms,
                  fill_ts_ms,
                  decision_ref_px,
                  fill_px,
                  qty,
                  slippage_bps,
                  fee_bps,
                  total_cost_bps,
                  alpha_remaining_at_fill,
                  ttl_ms,
                  age_ms,
                  aggressiveness,
                  order_type,
                  created_ts_ms,
                  meta_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(fill_ts_ms),
                    str(cid),
                    (str(broker) if broker is not None else None),
                    str(symbol),
                    int(submit_ts_ms),
                    int(fill_ts_ms),
                    float(ref_px),
                    float(fill_px),
                    float(qty),
                    float(slippage_bps),
                    float(fee_bps),
                    float(total_cost_bps),
                    float(alpha_rem),
                    int(ttl_ms),
                    int(age_ms),
                    aggressiveness,
                    order_type,
                    _now_ms(),
                    json.dumps(extra, separators=(",", ":"), sort_keys=True),
                ),
            )

            wrote += 1

        con.commit()

        try:
            _update_slippage_feedback(con, lookback_n=min(5000, int(limit) * 3))
        except Exception:
            pass
        try:
            _build_alpha_preservation_kpis(con, lookback_n=min(5000, int(limit) * 3))
        except Exception:
            pass

        return {
            "ok": True,
            "status": "built",
            "rows_written": int(wrote),
        }

    finally:
        con.close()


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    p = float(p)
    p = max(0.0, min(1.0, p))
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round((len(sorted_vals) - 1) * p))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return float(sorted_vals[idx])


def _update_slippage_feedback(con, lookback_n: int = 2000) -> None:
    """
    Rolling realized slippage → feedback knobs for execution policy.
    Writes one row per (broker, order_type, aggressiveness).
    """
    lookback_n = int(max(50, min(20000, int(lookback_n))))

    rows = con.execute(
        """
        SELECT
          ts_ms, broker, order_type, aggressiveness, meta_json, slippage_bps
        FROM execution_analytics
        WHERE slippage_bps IS NOT NULL
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (lookback_n,),
    ).fetchall()

    buckets: Dict[tuple, List[float]] = {}

    for ts_ms, broker, order_type, aggressiveness, meta_json, sl_bps in rows or []:
        try:
            b = str(broker or "").strip().lower() or None

            ot = str(order_type or "").upper().strip() or "UNKNOWN"
            ag = str(aggressiveness or "").upper().strip() or "UNKNOWN"

            # fallback parse if needed
            if (ot == "UNKNOWN" or ag == "UNKNOWN") and meta_json:
                try:
                    ex = json.loads(meta_json or "{}")
                    if isinstance(ex, dict):
                        ot = str(ex.get("order_type") or ot).upper().strip() or ot
                        ag = str(ex.get("aggressiveness") or ag).upper().strip() or ag
                except Exception:
                    pass

            key = (b, ot, ag)
            buckets.setdefault(key, []).append(float(sl_bps))
        except Exception:
            continue

    now_ms = _now_ms()

    for (b, ot, ag), vals in buckets.items():
        vals2 = sorted([float(v) for v in vals if v is not None])
        if len(vals2) < 10:
            continue

        med = _percentile(vals2, 0.50)
        p75 = _percentile(vals2, 0.75)

        suggested_limit_offset = float(max(0.0, (p75 or 0.0)))
        suggested_extra_slip = float(max(0.0, (med or 0.0)))

        con.execute(
            """
            INSERT INTO execution_slippage_feedback(
              ts_ms, broker, order_type, aggressiveness,
              sample_n, median_slippage_bps, p75_slippage_bps,
              suggested_limit_offset_bps, suggested_extra_slip_bps,
              extra_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                (str(b) if b else None),
                str(ot),
                str(ag),
                int(len(vals2)),
                (float(med) if med is not None else None),
                (float(p75) if p75 is not None else None),
                float(suggested_limit_offset),
                float(suggested_extra_slip),
                json.dumps(
                    {"lookback_n": int(lookback_n)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    con.commit()


def get_slippage_feedback(con, broker: str) -> Dict[str, Dict[str, float]]:
    """
    Returns: { "<ORDER_TYPE>|<AGGR>": {"limit_offset_bps": x, "extra_slip_bps": y} }
    Uses the most recent feedback row per key for the given broker.
    """
    b = str(broker or "").strip().lower()
    out: Dict[str, Dict[str, float]] = {}

    rows = con.execute(
        """
        SELECT order_type, aggressiveness, suggested_limit_offset_bps, suggested_extra_slip_bps
        FROM execution_slippage_feedback
        WHERE broker = ?
        ORDER BY ts_ms DESC
        LIMIT 500
        """,
        (b,),
    ).fetchall()

    for ot, ag, lo, es in rows or []:
        key = f"{str(ot)}|{str(ag)}"
        if key in out:
            continue
        try:
            out[key] = {
                "limit_offset_bps": float(lo or 0.0),
                "extra_slip_bps": float(es or 0.0),
            }
        except Exception:
            continue

    return out


def _build_alpha_preservation_kpis(con, lookback_n: int = 2000) -> None:
    """
    Alpha preservation KPI engine:
    - alpha_remaining_at_fill vs total execution cost
    - produces an efficiency score (alpha_remaining / (1 + cost))
    """
    lookback_n = int(max(50, min(20000, int(lookback_n))))

    rows = con.execute(
        """
        SELECT
          ts_ms, symbol, broker, total_cost_bps, alpha_remaining_at_fill, order_type, aggressiveness, meta_json
        FROM execution_analytics
        WHERE total_cost_bps IS NOT NULL
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (lookback_n,),
    ).fetchall()

    buckets: Dict[tuple, List[tuple]] = {}

    for ts_ms, sym, broker, cost_bps, a_rem, order_type, aggressiveness, meta_json in rows or []:
        try:
            ot = str(order_type or "").upper().strip() or "UNKNOWN"
            ag = str(aggressiveness or "").upper().strip() or "UNKNOWN"

            if (ot == "UNKNOWN" or ag == "UNKNOWN") and meta_json:
                try:
                    ex = json.loads(meta_json or "{}")
                    if isinstance(ex, dict):
                        ot = str(ex.get("order_type") or ot).upper().strip() or ot
                        ag = str(ex.get("aggressiveness") or ag).upper().strip() or ag
                except Exception:
                    pass

            key = (
                str(broker or "").strip().lower() or None,
                str(sym or "").upper().strip() or None,
                ot,
                ag,
            )
            buckets.setdefault(key, []).append(
                (float(a_rem or 0.0), float(cost_bps or 0.0))
            )
        except Exception:
            continue

    now_ms = _now_ms()

    for (b, sym, ot, ag), pts in buckets.items():
        if not sym or len(pts) < 10:
            continue

        a_vals = [p[0] for p in pts]
        c_vals = [p[1] for p in pts]

        avg_a = sum(a_vals) / float(len(a_vals))
        avg_c = sum(c_vals) / float(len(c_vals))

        eff = float(avg_a) / float(1.0 + max(0.0, avg_c) / 100.0)

        con.execute(
            """
            INSERT INTO alpha_preservation_kpis(
              ts_ms, broker, symbol, order_type, aggressiveness,
              sample_n, avg_alpha_remaining, avg_total_cost_bps,
              alpha_cost_efficiency, extra_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                (str(b) if b else None),
                str(sym),
                str(ot),
                str(ag),
                int(len(pts)),
                float(avg_a),
                float(avg_c),
                float(eff),
                json.dumps(
                    {"lookback_n": int(lookback_n)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    con.commit()



# REPLACE WITH (entire file)
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


def _ensure_tables(con):
    con.executescript(
        """
-- ============================================================
-- Canonical execution analytics table (aligned with code below)
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_analytics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,                 -- primary event timestamp (fill_ts_ms)
  client_order_id TEXT NOT NULL,
  broker TEXT,
  symbol TEXT NOT NULL,

  submit_ts_ms INTEGER,
  fill_ts_ms INTEGER,

  decision_ref_px REAL,                   -- reference/decision price at submit
  fill_px REAL,
  qty REAL,

  slippage_bps REAL,
  fee_bps REAL,
  total_cost_bps REAL,                    -- slippage + fees (best-effort)

  alpha_remaining_at_fill REAL,
  ttl_ms INTEGER,
  age_ms INTEGER,                         -- fill_ts_ms - submit_ts_ms

  aggressiveness TEXT,
  order_type TEXT,

  created_ts_ms INTEGER,                  -- insertion time
  meta_json TEXT                          -- serialized extra/meta blob
);

CREATE INDEX IF NOT EXISTS idx_execution_analytics_ts ON execution_analytics(ts_ms);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_symbol ON execution_analytics(symbol);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_broker ON execution_analytics(broker);
CREATE INDEX IF NOT EXISTS idx_execution_analytics_cid ON execution_analytics(client_order_id);

-- ---------------------------------------------
-- Slippage feedback loop (rolling realized costs)
-- ---------------------------------------------
CREATE TABLE IF NOT EXISTS execution_slippage_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  broker TEXT,
  order_type TEXT,
  aggressiveness TEXT,
  sample_n INTEGER NOT NULL,
  median_slippage_bps REAL,
  p75_slippage_bps REAL,
  suggested_limit_offset_bps REAL,
  suggested_extra_slip_bps REAL,
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_exec_slip_fb_ts ON execution_slippage_feedback(ts_ms);
CREATE INDEX IF NOT EXISTS idx_exec_slip_fb_broker ON execution_slippage_feedback(broker);

-- ---------------------------------------------
-- Alpha preservation KPIs (post-trade diagnostics)
-- ---------------------------------------------
CREATE TABLE IF NOT EXISTS alpha_preservation_kpis (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  broker TEXT,
  symbol TEXT,
  order_type TEXT,
  aggressiveness TEXT,
  sample_n INTEGER NOT NULL,
  avg_alpha_remaining REAL,
  avg_total_cost_bps REAL,
  alpha_cost_efficiency REAL,
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_alpha_kpis_ts ON alpha_preservation_kpis(ts_ms);
CREATE INDEX IF NOT EXISTS idx_alpha_kpis_broker ON alpha_preservation_kpis(broker);
CREATE INDEX IF NOT EXISTS idx_alpha_kpis_symbol ON alpha_preservation_kpis(symbol);
        """
    )


def _alpha_remaining(age_ms: int, half_life_ms: int, ttl_ms: int) -> float:
    if ttl_ms <= 0:
        return 0.0
    if age_ms >= ttl_ms:
        return 0.0
    hl = max(1, int(half_life_ms))
    decay = math.pow(0.5, float(age_ms) / float(hl))
    ttl_factor = max(0.0, 1.0 - (float(age_ms) / float(ttl_ms)))
    return max(0.0, min(1.0, decay * ttl_factor))


def summarize_execution_performance(days: int = 7) -> Dict[str, Any]:
    con = connect()
    try:
        _ensure_tables(con)
        since = _now_ms() - (int(days) * 86400000)

        rows = con.execute(
            """
            SELECT
              broker,
              symbol,
              COUNT(*) AS n,
              AVG(slippage_bps) AS avg_slip,
              AVG(alpha_remaining_at_fill) AS avg_alpha,
              AVG(age_ms) AS avg_age
            FROM execution_analytics
            WHERE ts_ms >= ?
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
                    "fills": int(n or 0),
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

        wrote = 0

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

            if qty == 0.0 or ref_px <= 0.0 or fill_px <= 0.0 or fill_ts_ms <= 0:
                continue

            side_sign = 1.0 if qty > 0 else -1.0
            slippage_bps = ((fill_px - ref_px) / ref_px) * 10000.0 * side_sign

            age_ms = max(0, int(fill_ts_ms) - int(submit_ts_ms))

            extra: Dict[str, Any]
            try:
                extra = json.loads(extra_json or "{}")
                if not isinstance(extra, dict):
                    extra = {}
            except Exception:
                extra = {}

            ttl_ms = int(extra.get("alpha_ttl_ms") or 0)
            half_life_ms = int(extra.get("alpha_half_life_ms") or 60000)

            alpha_rem = _alpha_remaining(age_ms, half_life_ms, ttl_ms)

            aggressiveness = str(extra.get("aggressiveness") or "")
            order_type = str(extra.get("order_type") or "")

            fee_bps = 0.0
            try:
                fee_bps = float(extra.get("fee_bps") or 0.0)
            except Exception:
                fee_bps = 0.0

            total_cost_bps = float(slippage_bps) + float(fee_bps)

            con.execute(
                """
                INSERT INTO execution_analytics(
                  ts_ms,
                  client_order_id,
                  broker,
                  symbol,
                  submit_ts_ms,
                  fill_ts_ms,
                  decision_ref_px,
                  fill_px,
                  qty,
                  slippage_bps,
                  fee_bps,
                  total_cost_bps,
                  alpha_remaining_at_fill,
                  ttl_ms,
                  age_ms,
                  aggressiveness,
                  order_type,
                  created_ts_ms,
                  meta_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(fill_ts_ms),
                    str(cid),
                    (str(broker) if broker is not None else None),
                    str(symbol),
                    int(submit_ts_ms),
                    int(fill_ts_ms),
                    float(ref_px),
                    float(fill_px),
                    float(qty),
                    float(slippage_bps),
                    float(fee_bps),
                    float(total_cost_bps),
                    float(alpha_rem),
                    int(ttl_ms),
                    int(age_ms),
                    aggressiveness,
                    order_type,
                    _now_ms(),
                    json.dumps(extra, separators=(",", ":"), sort_keys=True),
                ),
            )

            wrote += 1

        con.commit()

        try:
            _update_slippage_feedback(con, lookback_n=min(5000, int(limit) * 3))
        except Exception:
            pass
        try:
            _build_alpha_preservation_kpis(con, lookback_n=min(5000, int(limit) * 3))
        except Exception:
            pass

        return {
            "ok": True,
            "status": "built",
            "rows_written": int(wrote),
        }

    finally:
        con.close()


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    if not sorted_vals:
        return None
    p = float(p)
    p = max(0.0, min(1.0, p))
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round((len(sorted_vals) - 1) * p))
    idx = max(0, min(len(sorted_vals) - 1, idx))
    return float(sorted_vals[idx])


def _update_slippage_feedback(con, lookback_n: int = 2000) -> None:
    """
    Rolling realized slippage → feedback knobs for execution policy.
    Writes one row per (broker, order_type, aggressiveness).
    """
    lookback_n = int(max(50, min(20000, int(lookback_n))))

    rows = con.execute(
        """
        SELECT
          ts_ms, broker, order_type, aggressiveness, meta_json, slippage_bps
        FROM execution_analytics
        WHERE slippage_bps IS NOT NULL
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (lookback_n,),
    ).fetchall()

    buckets: Dict[tuple, List[float]] = {}

    for ts_ms, broker, order_type, aggressiveness, meta_json, sl_bps in rows or []:
        try:
            b = str(broker or "").strip().lower() or None

            ot = str(order_type or "").upper().strip() or "UNKNOWN"
            ag = str(aggressiveness or "").upper().strip() or "UNKNOWN"

            if (ot == "UNKNOWN" or ag == "UNKNOWN") and meta_json:
                try:
                    ex = json.loads(meta_json or "{}")
                    if isinstance(ex, dict):
                        ot = str(ex.get("order_type") or ot).upper().strip() or ot
                        ag = str(ex.get("aggressiveness") or ag).upper().strip() or ag
                except Exception:
                    pass

            key = (b, ot, ag)
            buckets.setdefault(key, []).append(float(sl_bps))
        except Exception:
            continue

    now_ms = _now_ms()

    for (b, ot, ag), vals in buckets.items():
        vals2 = sorted([float(v) for v in vals if v is not None])
        if len(vals2) < 10:
            continue

        med = _percentile(vals2, 0.50)
        p75 = _percentile(vals2, 0.75)

        suggested_limit_offset = float(max(0.0, (p75 or 0.0)))
        suggested_extra_slip = float(max(0.0, (med or 0.0)))

        con.execute(
            """
            INSERT INTO execution_slippage_feedback(
              ts_ms, broker, order_type, aggressiveness,
              sample_n, median_slippage_bps, p75_slippage_bps,
              suggested_limit_offset_bps, suggested_extra_slip_bps,
              extra_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                (str(b) if b else None),
                str(ot),
                str(ag),
                int(len(vals2)),
                (float(med) if med is not None else None),
                (float(p75) if p75 is not None else None),
                float(suggested_limit_offset),
                float(suggested_extra_slip),
                json.dumps(
                    {"lookback_n": int(lookback_n)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    con.commit()


def get_slippage_feedback(con, broker: str) -> Dict[str, Dict[str, float]]:
    """
    Returns: { "<ORDER_TYPE>|<AGGR>": {"limit_offset_bps": x, "extra_slip_bps": y} }
    Uses the most recent feedback row per key for the given broker.
    """
    b = str(broker or "").strip().lower()
    out: Dict[str, Dict[str, float]] = {}

    rows = con.execute(
        """
        SELECT order_type, aggressiveness, suggested_limit_offset_bps, suggested_extra_slip_bps
        FROM execution_slippage_feedback
        WHERE broker = ?
        ORDER BY ts_ms DESC
        LIMIT 500
        """,
        (b,),
    ).fetchall()

    for ot, ag, lo, es in rows or []:
        key = f"{str(ot)}|{str(ag)}"
        if key in out:
            continue
        try:
            out[key] = {
                "limit_offset_bps": float(lo or 0.0),
                "extra_slip_bps": float(es or 0.0),
            }
        except Exception:
            continue

    return out


def _build_alpha_preservation_kpis(con, lookback_n: int = 2000) -> None:
    """
    Alpha preservation KPI engine:
    - alpha_remaining_at_fill vs total execution cost
    - produces an efficiency score (alpha_remaining / (1 + cost))
    """
    lookback_n = int(max(50, min(20000, int(lookback_n))))

    rows = con.execute(
        """
        SELECT
          ts_ms, symbol, broker, total_cost_bps, alpha_remaining_at_fill, order_type, aggressiveness, meta_json
        FROM execution_analytics
        WHERE total_cost_bps IS NOT NULL
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (lookback_n,),
    ).fetchall()

    buckets: Dict[tuple, List[tuple]] = {}

    for ts_ms, sym, broker, cost_bps, a_rem, order_type, aggressiveness, meta_json in rows or []:
        try:
            ot = str(order_type or "").upper().strip() or "UNKNOWN"
            ag = str(aggressiveness or "").upper().strip() or "UNKNOWN"

            if (ot == "UNKNOWN" or ag == "UNKNOWN") and meta_json:
                try:
                    ex = json.loads(meta_json or "{}")
                    if isinstance(ex, dict):
                        ot = str(ex.get("order_type") or ot).upper().strip() or ot
                        ag = str(ex.get("aggressiveness") or ag).upper().strip() or ag
                except Exception:
                    pass

            key = (
                str(broker or "").strip().lower() or None,
                str(sym or "").upper().strip() or None,
                ot,
                ag,
            )
            buckets.setdefault(key, []).append(
                (float(a_rem or 0.0), float(cost_bps or 0.0))
            )
        except Exception:
            continue

    now_ms = _now_ms()

    for (b, sym, ot, ag), pts in buckets.items():
        if not sym or len(pts) < 10:
            continue

        a_vals = [p[0] for p in pts]
        c_vals = [p[1] for p in pts]

        avg_a = sum(a_vals) / float(len(a_vals))
        avg_c = sum(c_vals) / float(len(c_vals))

        eff = float(avg_a) / float(1.0 + max(0.0, avg_c) / 100.0)

        con.execute(
            """
            INSERT INTO alpha_preservation_kpis(
              ts_ms, broker, symbol, order_type, aggressiveness,
              sample_n, avg_alpha_remaining, avg_total_cost_bps,
              alpha_cost_efficiency, extra_json
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                (str(b) if b else None),
                str(sym),
                str(ot),
                str(ag),
                int(len(pts)),
                float(avg_a),
                float(avg_c),
                float(eff),
                json.dumps(
                    {"lookback_n": int(lookback_n)},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )

    con.commit()
