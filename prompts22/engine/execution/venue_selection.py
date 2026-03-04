import os
import json
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    from engine.storage import connect  # type: ignore
except Exception:
    connect = None  # type: ignore


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.executescript(
        """
CREATE TABLE IF NOT EXISTS execution_venue_feedback (
  ts_ms INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  instrument_type TEXT NOT NULL,
  venue TEXT NOT NULL,
  n INTEGER NOT NULL,
  avg_total_cost_bps REAL,
  avg_slippage_bps REAL,
  avg_fee_bps REAL,
  win_rate REAL,
  extra_json TEXT,
  PRIMARY KEY (ts_ms, symbol, instrument_type, venue)
);

CREATE INDEX IF NOT EXISTS idx_exec_venue_fb_sym
  ON execution_venue_feedback(symbol);

CREATE INDEX IF NOT EXISTS idx_exec_venue_fb_ts
  ON execution_venue_feedback(ts_ms);
"""
    )


def choose_best_venue(
    *,
    symbol: str,
    instrument_type: str,
    candidates: List[str],
    lookback_days: int = 14,
    min_n: int = 25,
    default_venue: Optional[str] = None,
    con=None,
) -> Tuple[str, Dict[str, Any]]:
    sym = str(symbol or "").upper().strip()
    it = str(instrument_type or "").upper().strip()
    cands = [str(v).upper().strip() for v in (candidates or []) if str(v or "").strip()]

    if not sym or not it or not cands:
        v = str(default_venue or (cands[0] if cands else ""))
        return v, {"ok": False, "reason": "missing_inputs"}

    if connect is None and con is None:
        v = str(default_venue or cands[0])
        return v, {"ok": False, "reason": "db_unavailable"}

    owns = False
    if con is None:
        con = connect(readonly=True)
        owns = True

    try:
        try:
            _ensure_tables(con)
        except Exception:
            pass

        now_ms = _now_ms()
        since_ms = now_ms - int(max(1, int(lookback_days))) * 24 * 60 * 60 * 1000

        venue_cost: Dict[str, Dict[str, Any]] = {}

        # Prefer learned_cost_model when available; it is state-conditional and updates online.
        # Fail-soft: any schema mismatch falls back to raw execution_analytics.
        try:
            chk = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='learned_cost_model'"
            ).fetchone()
        except Exception:
            chk = None

        use_learned = bool(chk)

        for v in cands:
            if use_learned:
                try:
                    row = con.execute(
                        """
                        SELECT SUM(n) as n_sum,
                               SUM(n * mean_realized_cost_bps) as w_sum
                        FROM learned_cost_model
                        WHERE symbol = ?
                          AND venue = ?
                          AND instrument_type = ?
                          AND last_update_ts_ms >= ?
                        """,
                        (sym, str(v), str(it), int(since_ms)),
                    ).fetchone()

                    n_sum = int((row[0] or 0) if row else 0)
                    w_sum = float((row[1] or 0.0) if row else 0.0)

                    if n_sum >= int(min_n) and n_sum > 0:
                        venue_cost[str(v)] = {
                            "n": int(n_sum),
                            "avg_total_cost_bps": float(w_sum) / float(n_sum),
                            "source": "learned_cost_model",
                        }
                        continue
                except Exception:
                    # schema mismatch -> disable learned path globally
                    use_learned = False

            try:
                rows = con.execute(
                    """
                    SELECT total_cost_bps, slippage_bps, fee_bps
                    FROM execution_analytics
                    WHERE symbol = ?
                      AND ts_ms >= ?
                      AND total_cost_bps IS NOT NULL
                      AND meta_json IS NOT NULL
                      AND json_extract(meta_json, '$.venue') = ?
                      AND json_extract(meta_json, '$.instrument_type') = ?
                    ORDER BY ts_ms DESC
                    LIMIT 2000
                    """,
                    (sym, int(since_ms), str(v), str(it)),
                ).fetchall() or []
            except Exception:
                rows = []

            if not rows:
                continue

            costs: List[float] = []
            slips: List[float] = []
            fees: List[float] = []
            for r in rows:
                try:
                    c = float(r[0])
                    s = float(r[1]) if r[1] is not None else 0.0
                    f = float(r[2]) if r[2] is not None else 0.0
                except Exception:
                    continue
                costs.append(c)
                slips.append(s)
                fees.append(f)

            n = int(len(costs))
            if n <= 0:
                continue

            avg_cost = float(sum(costs) / float(n))
            avg_slip = float(sum(slips) / float(n))
            avg_fee = float(sum(fees) / float(n))
            win_rate = float(sum(1.0 for x in costs if x <= 0.0) / float(n))

            venue_cost[str(v)] = {
                "n": n,
                "avg_total_cost_bps": avg_cost,
                "avg_slippage_bps": avg_slip,
                "avg_fee_bps": avg_fee,
                "win_rate": win_rate,
                "source": "execution_analytics",
            }

        if not venue_cost:
            v = str(default_venue or cands[0])
            return v, {"ok": False, "reason": "no_history"}

        eligible = {k: d for (k, d) in venue_cost.items() if int(d.get("n") or 0) >= int(min_n)}
        if not eligible:
            v = str(default_venue or cands[0])
            return v, {"ok": False, "reason": "insufficient_history", "stats": venue_cost}

        best = min(eligible.items(), key=lambda kv: float(kv[1].get("avg_total_cost_bps") or 0.0))
        return str(best[0]), {"ok": True, "stats": venue_cost, "chosen": str(best[0])}

    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def write_feedback_snapshot(
    *,
    ts_ms: Optional[int] = None,
    lookback_days: int = 14,
    con=None,
) -> Dict[str, Any]:
    if connect is None and con is None:
        return {"ok": False, "status": "db_unavailable"}

    owns = False
    if con is None:
        con = connect()
        owns = True

    try:
        _ensure_tables(con)
        now_ms = int(ts_ms or _now_ms())
        since_ms = now_ms - int(max(1, int(lookback_days))) * 24 * 60 * 60 * 1000

        rows = con.execute(
            """
            SELECT
              symbol,
              json_extract(meta_json, '$.instrument_type') as instrument_type,
              json_extract(meta_json, '$.venue') as venue,
              total_cost_bps,
              slippage_bps,
              fee_bps
            FROM execution_analytics
            WHERE ts_ms >= ?
              AND total_cost_bps IS NOT NULL
              AND meta_json IS NOT NULL
              AND json_extract(meta_json, '$.venue') IS NOT NULL
              AND json_extract(meta_json, '$.instrument_type') IS NOT NULL
            ORDER BY ts_ms DESC
            LIMIT 100000
            """,
            (int(since_ms),),
        ).fetchall() or []

        buckets: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

        for sym, it, venue, cost, slip, fee in rows:
            k = (str(sym or "").upper().strip(), str(it or "").upper().strip(), str(venue or "").upper().strip())
            if not k[0] or not k[1] or not k[2]:
                continue
            b = buckets.get(k)
            if b is None:
                b = {"n": 0, "cost": 0.0, "slip": 0.0, "fee": 0.0, "wins": 0}
                buckets[k] = b
            try:
                c = float(cost)
                s = float(slip) if slip is not None else 0.0
                f = float(fee) if fee is not None else 0.0
            except Exception:
                continue
            b["n"] += 1
            b["cost"] += c
            b["slip"] += s
            b["fee"] += f
            if c <= 0.0:
                b["wins"] += 1

        wrote = 0
        for (sym, it, venue), agg in buckets.items():
            n = int(agg.get("n") or 0)
            if n <= 0:
                continue
            avg_cost = float(agg.get("cost") or 0.0) / float(n)
            avg_slip = float(agg.get("slip") or 0.0) / float(n)
            avg_fee = float(agg.get("fee") or 0.0) / float(n)
            win_rate = float(int(agg.get("wins") or 0) / float(n))

            con.execute(
                """
                INSERT OR REPLACE INTO execution_venue_feedback(
                  ts_ms, symbol, instrument_type, venue,
                  n, avg_total_cost_bps, avg_slippage_bps, avg_fee_bps, win_rate,
                  extra_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(now_ms),
                    str(sym),
                    str(it),
                    str(venue),
                    int(n),
                    float(avg_cost),
                    float(avg_slip),
                    float(avg_fee),
                    float(win_rate),
                    json.dumps({"lookback_days": int(lookback_days)}, separators=(",", ":"), sort_keys=True),
                ),
            )
            wrote += 1

        try:
            con.commit()
        except Exception:
            pass

        return {"ok": True, "status": "ok", "wrote": int(wrote), "buckets": int(len(buckets))}

    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass
