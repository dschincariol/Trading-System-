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

from engine.storage import connect


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
  client_order_id TEXT NOT NULL UNIQUE,
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

CREATE TABLE IF NOT EXISTS realized_cost_observations (
  ts_ms INTEGER NOT NULL,
  client_order_id TEXT NOT NULL,
  broker TEXT,
  symbol TEXT NOT NULL,
  venue TEXT,
  instrument_type TEXT,
  order_qty REAL,
  volatility REAL,
  spread_bps REAL,
  realized_cost_bps REAL,
  features_json TEXT,
  PRIMARY KEY (ts_ms, client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_realized_cost_obs_sym_ts ON realized_cost_observations(symbol, ts_ms);
CREATE INDEX IF NOT EXISTS idx_realized_cost_obs_venue_ts ON realized_cost_observations(venue, ts_ms);

CREATE TABLE IF NOT EXISTS learned_cost_model (
  asof_ts_ms INTEGER NOT NULL,
  broker TEXT,
  symbol TEXT NOT NULL,
  venue TEXT,
  instrument_type TEXT,
  vol_bucket TEXT,
  liq_bucket TEXT,
  size_bucket TEXT,
  n INTEGER NOT NULL,
  mean_realized_cost_bps REAL NOT NULL,
  m2_realized_cost_bps REAL NOT NULL,
  last_update_ts_ms INTEGER NOT NULL,
  PRIMARY KEY (broker, symbol, venue, instrument_type, vol_bucket, liq_bucket, size_bucket)
);

CREATE INDEX IF NOT EXISTS idx_learned_cost_model_sym_venue ON learned_cost_model(symbol, venue);
CREATE INDEX IF NOT EXISTS idx_learned_cost_model_update_ts ON learned_cost_model(last_update_ts_ms);
        """
    )


def _bucket_vol(v: Optional[float]) -> str:
    try:
        x = float(v or 0.0)
    except Exception:
        return "v_unk"
    if x <= 0:
        return "v_0"
    if x < 0.01:
        return "v_1"
    if x < 0.02:
        return "v_2"
    if x < 0.035:
        return "v_3"
    return "v_4"


def _bucket_spread_bps(v: Optional[float]) -> str:
    try:
        x = float(v or 0.0)
    except Exception:
        return "s_unk"
    if x <= 0:
        return "s_0"
    if x < 2.0:
        return "s_1"
    if x < 5.0:
        return "s_2"
    if x < 12.0:
        return "s_3"
    return "s_4"


def _bucket_size(qty: Optional[float]) -> str:
    try:
        q = abs(float(qty or 0.0))
    except Exception:
        return "q_unk"
    if q <= 0:
        return "q_0"
    if q <= 1:
        return "q_1"
    if q <= 10:
        return "q_2"
    if q <= 100:
        return "q_3"
    if q <= 1000:
        return "q_4"
    return "q_5"


def _update_learned_cost_model(
    *,
    con,
    asof_ts_ms: int,
    broker: Optional[str],
    symbol: str,
    venue: Optional[str],
    instrument_type: Optional[str],
    vol_bucket: str,
    liq_bucket: str,
    size_bucket: str,
    realized_cost_bps: float,
) -> None:
    b = (str(broker).lower().strip() if broker else None)
    sym = str(symbol or "").upper().strip()
    v = (str(venue).upper().strip() if venue else None)
    it = (str(instrument_type).upper().strip() if instrument_type else None)
    vb = str(vol_bucket or "v_unk")
    lb = str(liq_bucket or "s_unk")
    qb = str(size_bucket or "q_unk")

    if not sym:
        return

    row = con.execute(
        """
        SELECT n, mean_realized_cost_bps, m2_realized_cost_bps
        FROM learned_cost_model
        WHERE (broker IS ? OR broker = ?)
          AND symbol = ?
          AND (venue IS ? OR venue = ?)
          AND (instrument_type IS ? OR instrument_type = ?)
          AND vol_bucket = ?
          AND liq_bucket = ?
          AND size_bucket = ?
        LIMIT 1
        """,
        (b, b, sym, v, v, it, it, vb, lb, qb),
    ).fetchone()

    if row:
        n0 = int(row[0] or 0)
        mu0 = float(row[1] or 0.0)
        m20 = float(row[2] or 0.0)
    else:
        n0 = 0
        mu0 = 0.0
        m20 = 0.0

    x = float(realized_cost_bps)
    n1 = n0 + 1

    if n0 <= 0:
        mu1 = x
        m21 = 0.0
    else:
        delta = x - mu0
        mu1 = mu0 + (delta / float(n1))
        delta2 = x - mu1
        m21 = m20 + (delta * delta2)

    con.execute(
        """
        INSERT INTO learned_cost_model(
          asof_ts_ms, broker, symbol, venue, instrument_type,
          vol_bucket, liq_bucket, size_bucket,
          n, mean_realized_cost_bps, m2_realized_cost_bps, last_update_ts_ms
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(broker, symbol, venue, instrument_type, vol_bucket, liq_bucket, size_bucket)
        DO UPDATE SET
          asof_ts_ms=excluded.asof_ts_ms,
          n=excluded.n,
          mean_realized_cost_bps=excluded.mean_realized_cost_bps,
          m2_realized_cost_bps=excluded.m2_realized_cost_bps,
          last_update_ts_ms=excluded.last_update_ts_ms
        """,
        (
            int(asof_ts_ms),
            b,
            sym,
            v,
            it,
            vb,
            lb,
            qb,
            int(n1),
            float(mu1),
            float(m21),
            int(_now_ms()),
        ),
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
              f.fill_qty,
              s.extra_json
            FROM execution_orders s
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
                order_qty,
                submit_ts_ms,
                ref_px,
                fill_ts_ms,
                fill_px,
                fill_qty,
                extra_json,
            ) = r

            try:
                order_qty = float(order_qty or 0.0)
                fill_qty = float(fill_qty or 0.0)
                ref_px = float(ref_px or 0.0)
                fill_px = float(fill_px or 0.0)
                submit_ts_ms = int(submit_ts_ms or 0)
                fill_ts_ms = int(fill_ts_ms or 0)
            except Exception:
                continue

            if order_qty == 0.0 or fill_qty <= 0.0 or ref_px <= 0.0 or fill_px <= 0.0 or fill_ts_ms <= 0:
                continue

            side_sign = 1.0 if order_qty > 0 else -1.0
            signed_qty = float(fill_qty) * float(side_sign)
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

            venue = None
            instrument_type = None
            volatility = None
            spread_bps = None
            try:
                venue = extra.get("venue")
            except Exception:
                venue = None
            try:
                instrument_type = extra.get("instrument_type")
            except Exception:
                instrument_type = None
            try:
                volatility = float(extra.get("volatility") or extra.get("sigma") or 0.0)
            except Exception:
                volatility = None
            try:
                spread_bps = float(extra.get("spread_bps") or extra.get("spread") or 0.0)
            except Exception:
                spread_bps = None

            con.execute(
                """
                INSERT OR IGNORE INTO execution_analytics(
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
                    float(signed_qty),
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

            try:
                con.execute(
                    """
                    INSERT OR IGNORE INTO realized_cost_observations(
                      ts_ms, client_order_id, broker, symbol, venue, instrument_type,
                      order_qty, volatility, spread_bps,
                      realized_cost_bps,
                      features_json
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        int(fill_ts_ms),
                        str(cid),
                        (str(broker).lower().strip() if broker is not None else None),
                        str(symbol).upper().strip(),
                        (str(venue).upper().strip() if venue else None),
                        (str(instrument_type).upper().strip() if instrument_type else None),
                        float(signed_qty),
                        (float(volatility) if volatility is not None else None),
                        (float(spread_bps) if spread_bps is not None else None),
                        float(total_cost_bps),
                        json.dumps(
                            {
                                "volatility": (float(volatility) if volatility is not None else None),
                                "spread_bps": (float(spread_bps) if spread_bps is not None else None),
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
            except Exception:
                pass

            try:
                _update_learned_cost_model(
                    con=con,
                    asof_ts_ms=int(fill_ts_ms),
                    broker=(str(broker).lower().strip() if broker is not None else None),
                    symbol=str(symbol),
                    venue=(str(venue) if venue else None),
                    instrument_type=(str(instrument_type) if instrument_type else None),
                    vol_bucket=_bucket_vol(volatility),
                    liq_bucket=_bucket_spread_bps(spread_bps),
                    size_bucket=_bucket_size(signed_qty),
                    realized_cost_bps=float(total_cost_bps),
                )
            except Exception:
                pass

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

        # ------------------------------------------------------------
        # Tail metrics: baseline vs adaptive (reads meta_json flag)
        # ------------------------------------------------------------
        adaptive_slips: List[float] = []
        baseline_slips: List[float] = []

        try:
            tail_rows = con.execute(
                """
                SELECT slippage_bps, meta_json
                FROM execution_analytics
                WHERE slippage_bps IS NOT NULL
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(max(500, min(20000, int(limit) * 4))),),
            ).fetchall()

            for sl, mj in tail_rows or []:
                try:
                    slv = float(sl)
                except Exception:
                    continue
                try:
                    meta = json.loads(mj or "{}")
                    if isinstance(meta, dict) and bool(meta.get("adaptive_slice", False)):
                        adaptive_slips.append(slv)
                    else:
                        baseline_slips.append(slv)
                except Exception:
                    baseline_slips.append(slv)

        except Exception:
            pass

        def _p(x: List[float], p: float) -> Optional[float]:
            if not x:
                return None
            x2 = sorted([float(v) for v in x])
            if not x2:
                return None
            if len(x2) == 1:
                return float(x2[0])
            idx = int(round((len(x2) - 1) * float(p)))
            idx = max(0, min(len(x2) - 1, idx))
            return float(x2[idx])

        return {
            "ok": True,
            "status": "built",
            "rows_written": int(wrote),
            "baseline_p95_slippage_bps": _p(baseline_slips, 0.95),
            "baseline_p99_slippage_bps": _p(baseline_slips, 0.99),
            "adaptive_p95_slippage_bps": _p(adaptive_slips, 0.95),
            "adaptive_p99_slippage_bps": _p(adaptive_slips, 0.99),
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
              f.fill_qty,
              s.extra_json
            FROM execution_orders s
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
                order_qty,
                submit_ts_ms,
                ref_px,
                fill_ts_ms,
                fill_px,
                fill_qty,
                extra_json,
            ) = r

            try:
                order_qty = float(order_qty or 0.0)
                fill_qty = float(fill_qty or 0.0)
                ref_px = float(ref_px or 0.0)
                fill_px = float(fill_px or 0.0)
                submit_ts_ms = int(submit_ts_ms or 0)
                fill_ts_ms = int(fill_ts_ms or 0)
            except Exception:
                continue

            if order_qty == 0.0 or fill_qty <= 0.0 or ref_px <= 0.0 or fill_px <= 0.0 or fill_ts_ms <= 0:
                continue

            side_sign = 1.0 if order_qty > 0 else -1.0
            signed_qty = float(fill_qty) * float(side_sign)
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
                INSERT OR IGNORE INTO execution_analytics(
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
                    float(signed_qty),
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

        # ------------------------------------------------------------
        # Tail metrics: baseline vs adaptive (reads meta_json flag)
        # ------------------------------------------------------------
        adaptive_slips: List[float] = []
        baseline_slips: List[float] = []

        try:
            tail_rows = con.execute(
                """
                SELECT slippage_bps, meta_json
                FROM execution_analytics
                WHERE slippage_bps IS NOT NULL
                ORDER BY ts_ms DESC
                LIMIT ?
                """,
                (int(max(500, min(20000, int(limit) * 4))),),
            ).fetchall()

            for sl, mj in tail_rows or []:
                try:
                    slv = float(sl)
                except Exception:
                    continue
                try:
                    meta = json.loads(mj or "{}")
                    if isinstance(meta, dict) and bool(meta.get("adaptive_slice", False)):
                        adaptive_slips.append(slv)
                    else:
                        baseline_slips.append(slv)
                except Exception:
                    baseline_slips.append(slv)

        except Exception:
            pass

        def _p(x: List[float], p: float) -> Optional[float]:
            if not x:
                return None
            x2 = sorted([float(v) for v in x])
            if not x2:
                return None
            if len(x2) == 1:
                return float(x2[0])
            idx = int(round((len(x2) - 1) * float(p)))
            idx = max(0, min(len(x2) - 1, idx))
            return float(x2[idx])

        return {
            "ok": True,
            "status": "built",
            "rows_written": int(wrote),
            "baseline_p95_slippage_bps": _p(baseline_slips, 0.95),
            "baseline_p99_slippage_bps": _p(baseline_slips, 0.99),
            "adaptive_p95_slippage_bps": _p(adaptive_slips, 0.95),
            "adaptive_p99_slippage_bps": _p(adaptive_slips, 0.99),
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

# ============================================================
# TSE SUPPORT FUNCTIONS
# ============================================================

def get_slippage_zscore(con):
    try:
        rows = con.execute(
            """
            SELECT slippage_bps
            FROM execution_analytics
            WHERE ts_ms >= (SELECT MAX(ts_ms) - 86400000 FROM execution_analytics)
            """
        ).fetchall()

        vals = [float(r[0]) for r in rows or [] if r and r[0] is not None]
        if len(vals) < 5:
            return 0.0

        mean = sum(vals) / float(len(vals))
        var = sum((x - mean) ** 2 for x in vals) / float(len(vals))
        std = var ** 0.5
        if std <= 1e-12:
            return 0.0

        latest = vals[0]
        return (float(latest) - mean) / std

    except Exception:
        return 0.0
        if not row:
            return 0.0
        mu = float(row[0] or 0.0)
        sigma = float(row[1] or 0.0)
        if sigma <= 1e-12:
            return 0.0
        latest = con.execute(
            "SELECT slippage_bps FROM execution_analytics ORDER BY ts_ms DESC LIMIT 1"
        ).fetchone()
        if not latest:
            return 0.0
        return (float(latest[0]) - mu) / sigma
    except Exception:
        return 0.0

def get_latency_variance_zscore(con):
    try:
        rows = con.execute(
            """
            SELECT age_ms
            FROM execution_analytics
            WHERE ts_ms >= (SELECT MAX(ts_ms) - 86400000 FROM execution_analytics)
            """
        ).fetchall()

        vals = [float(r[0]) for r in rows or [] if r and r[0] is not None]
        if len(vals) < 5:
            return 0.0

        mean = sum(vals) / float(len(vals))
        var = sum((x - mean) ** 2 for x in vals) / float(len(vals))
        std = var ** 0.5
        if std <= 1e-12:
            return 0.0

        latest = vals[0]
        return (float(latest) - mean) / std

    except Exception:
        return 0.0

# ============================================================
# Bayesian Rolling Expectancy (for TSE gating)
# ============================================================

def get_rolling_expectancy_stats(con, lookback_n: int = 100):
    """
    Returns:
        {
            mean: float,
            std: float,
            n: int,
            sharpe: float
        }
    Uses realized_pnl from execution_ledger if available.
    """
    try:
        rows = con.execute(
            """
            SELECT realized_pnl
            FROM execution_ledger
            WHERE realized_pnl IS NOT NULL
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(lookback_n),),
        ).fetchall()
        if not rows:
            return {"mean": 0.0, "std": 0.0, "n": 0, "sharpe": 0.0}

        vals = []
        for r in rows:
            try:
                vals.append(float(r[0]))
            except Exception:
                continue

        if not vals:
            return {"mean": 0.0, "std": 0.0, "n": 0, "sharpe": 0.0}

        n = len(vals)
        mean = sum(vals) / float(n)

        var = sum((x - mean) ** 2 for x in vals) / float(n) if n > 1 else 0.0
        std = var ** 0.5 if var > 0.0 else 0.0

        sharpe = (mean / std) * (n ** 0.5) if std > 1e-12 else 0.0

        return {
            "mean": float(mean),
            "std": float(std),
            "n": int(n),
            "sharpe": float(sharpe),
        }

    except Exception:
        return {"mean": 0.0, "std": 0.0, "n": 0, "sharpe": 0.0}

def get_expectancy_multiplier(con, lookback_n: int = 100) -> float:
    """
    Converts rolling expectancy into suppression multiplier.
    <1.0 tightens execution
    >1.0 loosens execution
    """
    stats = get_rolling_expectancy_stats(con, lookback_n=lookback_n)

    mean = float(stats.get("mean") or 0.0)
    sharpe = float(stats.get("sharpe") or 0.0)

    # Negative expectancy → tighten
    if mean < 0.0 and sharpe < 0.0:
        return 0.75

    # Strong positive expectancy → allow slight loosen
    if sharpe > 1.0:
        return 1.10

    return 1.0

# ============================================================
# Execution Degradation Snapshot (for auto-pause / TSE)
# ============================================================

def get_execution_degradation_snapshot(con, lookback_n: int = 500) -> Dict[str, Any]:
    """
    Computes:
      - rolling slippage mean
      - rolling latency mean
      - p95 slippage
      - p95 latency
    Used by TSE / EPE for hard auto-pause decisions.
    """
    try:
        rows = con.execute(
            """
            SELECT slippage_bps, age_ms
            FROM execution_analytics
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(max(50, lookback_n)),),
        ).fetchall()

        slips = []
        lats = []

        for sl, lat in rows or []:
            try:
                slips.append(float(sl))
            except Exception:
                pass
            try:
                lats.append(float(lat))
            except Exception:
                pass

        if not slips:
            return {
                "mean_slippage": 0.0,
                "p95_slippage": 0.0,
                "mean_latency": 0.0,
                "p95_latency": 0.0,
                "n": 0,
            }

        slips_sorted = sorted(slips)
        lats_sorted = sorted(lats)

        def _p(arr, p):
            if not arr:
                return 0.0
            idx = int(round((len(arr) - 1) * p))
            idx = max(0, min(len(arr) - 1, idx))
            return float(arr[idx])

        return {
            "mean_slippage": sum(slips) / float(len(slips)),
            "p95_slippage": _p(slips_sorted, 0.95),
            "mean_latency": (sum(lats) / float(len(lats)) if lats else 0.0),
            "p95_latency": _p(lats_sorted, 0.95) if lats else 0.0,
            "n": int(len(slips)),
        }

    except Exception:
        return {
            "mean_slippage": 0.0,
            "p95_slippage": 0.0,
            "mean_latency": 0.0,
            "p95_latency": 0.0,
            "n": 0,
        }

# ============================================================
# Adaptive Alpha Half-Life Learning
# ============================================================

def compute_adaptive_half_life(con, symbol: str, lookback_n: int = 500) -> Optional[int]:
    """
    Learns half-life based on alpha_remaining_at_fill decay profile.
    Returns suggested half-life in ms.
    """
    try:
        rows = con.execute(
            """
            SELECT age_ms, alpha_remaining_at_fill
            FROM execution_analytics
            WHERE symbol = ?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(symbol), int(max(50, lookback_n))),
        ).fetchall()

        pairs = [
            (float(age), float(a))
            for age, a in rows or []
            if age is not None and a is not None and a > 0.0
        ]

        if len(pairs) < 20:
            return None

        # Estimate half-life where alpha ≈ 0.5
        diffs = [(abs(a - 0.5), age) for age, a in pairs]
        diffs_sorted = sorted(diffs, key=lambda x: x[0])

        if not diffs_sorted:
            return None

        return int(diffs_sorted[0][1])

    except Exception:
        return None

# ============================================================
# Broker Performance Ranking
# ============================================================

def rank_brokers_by_cost(con, lookback_days: int = 7) -> List[Dict[str, Any]]:
    try:
        since = _now_ms() - (int(lookback_days) * 86400000)

        rows = con.execute(
            """
            SELECT broker,
                   COUNT(*) as n,
                   AVG(total_cost_bps) as avg_cost
            FROM execution_analytics
            WHERE ts_ms >= ?
            GROUP BY broker
            ORDER BY avg_cost ASC
            """,
            (int(since),),
        ).fetchall()

        out = []
        for broker, n, avg_cost in rows or []:
            out.append({
                "broker": broker,
                "fills": int(n or 0),
                "avg_total_cost_bps": float(avg_cost or 0.0),
            })

        return out

    except Exception:
        return []
