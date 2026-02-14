# dev_core/execution_ledger.py
"""
Execution ledger (SQLite):

- execution_orders: one row per submitted broker order (by client_order_id)
- execution_fills:  fills captured later (polling or event-driven)
- execution_metrics: slippage + mark-to-market PnL snapshots
- pnl_attribution: grouped PnL per source_alert_id (signal)

Designed to be broker-agnostic.
"""

import json
import time
from typing import Any, Dict, Optional, Tuple

from engine.dev_core.storage import connect

# Back-compat: some deployments reference this import elsewhere
try:
    from engine.dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot  # noqa: F401
except Exception:
    upsert_from_latest_pnl_attribution_snapshot = None  # type: ignore


SCHEMA = """
-- ============================================================
-- execution_orders (superset: old + new)
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_orders (
  client_order_id TEXT PRIMARY KEY,
  broker TEXT NOT NULL,
  portfolio_orders_id INTEGER,
  source_alert_id INTEGER,
  symbol TEXT NOT NULL,
  qty REAL NOT NULL,
  submit_ts_ms INTEGER NOT NULL,
  ref_px REAL,
  broker_order_id TEXT,
  status TEXT NOT NULL DEFAULT 'submitted',
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_orders_submit_ts
  ON execution_orders(submit_ts_ms);

CREATE INDEX IF NOT EXISTS idx_execution_orders_source_alert
  ON execution_orders(source_alert_id);

-- ============================================================
-- execution_fills (superset: supports old + new callers)
-- Old shape:
--   id AUTOINCREMENT, client_order_id, fill_ts_ms, fill_qty, fill_px,
--   fees, liquidity, raw_json
-- New shape:
--   (client_order_id, fill_id) PK, broker, symbol, qty, fill_px, fill_ts_ms, fees, extra_json
-- We keep a single table with AUTOINCREMENT + optional fill_id and other cols.
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL UNIQUE,
  fill_id TEXT,
  broker TEXT,
  symbol TEXT,
  fill_ts_ms INTEGER NOT NULL,
  fill_qty REAL NOT NULL,
  fill_px REAL NOT NULL,
  fees REAL,
  liquidity TEXT,
  raw_json TEXT,
  extra_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_fills_ts
  ON execution_fills(fill_ts_ms);

CREATE INDEX IF NOT EXISTS idx_execution_fills_client
  ON execution_fills(client_order_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_fills_client_fillid
  ON execution_fills(client_order_id, fill_id);

-- ============================================================
-- execution_metrics (superset: old + new)
-- Old shape:
--   ts_ms, client_order_id, symbol, ref_px, fill_vwap, slippage_bps, m2m_pnl, last_px
-- New shape adds:
--   broker, submit_qty, filled_qty, fees
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_metrics (
  ts_ms INTEGER NOT NULL,
  client_order_id TEXT NOT NULL UNIQUE,
  broker TEXT,
  symbol TEXT NOT NULL,
  submit_qty REAL,
  filled_qty REAL,
  ref_px REAL,
  fill_vwap REAL,
  slippage_bps REAL,
  fees REAL,
  m2m_pnl REAL,
  last_px REAL,
  PRIMARY KEY (ts_ms, client_order_id)
);

-- ============================================================
-- pnl_attribution (unchanged)
-- ============================================================
CREATE TABLE IF NOT EXISTS pnl_attribution (
  ts_ms INTEGER NOT NULL,
  source_alert_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  pnl REAL NOT NULL,
  fees REAL NOT NULL,
  slippage_bps REAL,
  extra_json TEXT,
  PRIMARY KEY (ts_ms, source_alert_id, symbol)
);

-- ============================================================
-- capital_efficiency (old table)
-- ============================================================
CREATE TABLE IF NOT EXISTS capital_efficiency (
  ts_ms INTEGER NOT NULL,
  source_alert_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  capital_hours REAL NOT NULL,
  return_per_risk REAL,
  drawdown_contribution REAL,
  efficiency_score REAL,
  extra_json TEXT,
  PRIMARY KEY (ts_ms, source_alert_id, symbol)
);

-- ============================================================
-- execution_capital_efficiency (new table)
-- ============================================================
CREATE TABLE IF NOT EXISTS execution_capital_efficiency (
  ts_ms INTEGER NOT NULL,
  client_order_id TEXT NOT NULL UNIQUE,
  broker TEXT,
  portfolio_orders_id INTEGER,
  source_alert_id INTEGER,
  strategy_name TEXT,
  symbol TEXT NOT NULL,
  submit_ts_ms INTEGER,
  filled_qty REAL,
  fill_vwap REAL,
  fees REAL,
  notional REAL,
  holding_hours REAL,
  capital_hours REAL,
  pnl_net REAL,
  return_per_risk REAL,
  drawdown_contrib REAL,
  efficiency_score REAL,
  extra_json TEXT,
  PRIMARY KEY (ts_ms, client_order_id)
);

CREATE INDEX IF NOT EXISTS idx_exec_cap_eff_ts
  ON execution_capital_efficiency(ts_ms);

CREATE INDEX IF NOT EXISTS idx_exec_cap_eff_strategy_ts
  ON execution_capital_efficiency(strategy_name, ts_ms);
"""


def init_execution_ledger() -> None:
    con = connect()
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def now_ms() -> int:
    return int(time.time() * 1000)


def log_submit(
    client_order_id: str,
    broker: str,
    symbol: str,
    qty: float,
    submit_ts_ms: int,
    ref_px: Optional[float] = None,
    broker_order_id: Optional[str] = None,
    portfolio_orders_id: Optional[int] = None,
    source_alert_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    init_execution_ledger()
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO execution_orders(
              client_order_id, broker, portfolio_orders_id, source_alert_id,
              symbol, qty, submit_ts_ms, ref_px, broker_order_id, status, extra_json
            )
            VALUES (?,?,?,?,?,?,?,?,?,'submitted',?)
            ON CONFLICT(client_order_id) DO UPDATE SET
              broker=excluded.broker,
              portfolio_orders_id=excluded.portfolio_orders_id,
              source_alert_id=excluded.source_alert_id,
              symbol=excluded.symbol,
              qty=excluded.qty,
              submit_ts_ms=excluded.submit_ts_ms,
              ref_px=excluded.ref_px,
              broker_order_id=excluded.broker_order_id,
              status=excluded.status,
              extra_json=excluded.extra_json
            """,
            (
                str(client_order_id),
                str(broker),
                int(portfolio_orders_id) if portfolio_orders_id is not None else None,
                int(source_alert_id) if source_alert_id is not None else None,
                str(symbol),
                float(qty),
                int(submit_ts_ms),
                float(ref_px) if ref_px is not None else None,
                str(broker_order_id) if broker_order_id is not None else None,
                json.dumps(extra or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()


def log_fill(*args, **kwargs) -> None:
    """
    Back/forward compatible wrapper.

    Old signature:
      log_fill(client_order_id, fill_ts_ms, fill_qty, fill_px, fees=None, liquidity=None, raw=None)

    New signature:
      log_fill(client_order_id, fill_id, broker, symbol, qty, fill_px, fill_ts_ms, fees=None, extra=None)
    """
    # If called with kwargs that include fill_id OR positional arg2 is str -> new
    if "fill_id" in kwargs or (len(args) >= 2 and isinstance(args[1], str)):
        _log_fill_v2(*args, **kwargs)
        return
    _log_fill_v1(*args, **kwargs)


def _log_fill_v1(
    client_order_id: str,
    fill_ts_ms: int,
    fill_qty: float,
    fill_px: float,
    fees: Optional[float] = None,
    liquidity: Optional[str] = None,
    raw: Optional[Dict[str, Any]] = None,
) -> None:
    init_execution_ledger()
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO execution_fills(
              client_order_id, fill_ts_ms, fill_qty, fill_px, fees, liquidity, raw_json, extra_json, fill_id, broker, symbol
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(client_order_id),
                int(fill_ts_ms),
                float(fill_qty),
                float(fill_px),
                float(fees) if fees is not None else None,
                str(liquidity) if liquidity is not None else None,
                json.dumps(raw or {}, separators=(",", ":"), sort_keys=True),
                None,
                None,
                None,
                None,
            ),
        )
        con.commit()
    finally:
        con.close()


def _log_fill_v2(
    client_order_id: str,
    fill_id: str,
    broker: str,
    symbol: str,
    qty: float,
    fill_px: float,
    fill_ts_ms: int,
    fees: Optional[float] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    init_execution_ledger()
    con = connect()
    try:
        # Prefer idempotency on (client_order_id, fill_id) when fill_id is present.
        if fill_id:
            con.execute(
                """
                INSERT OR IGNORE INTO execution_fills(
                  client_order_id, fill_id, broker, symbol, fill_ts_ms, fill_qty, fill_px, fees, extra_json
                )
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(client_order_id),
                    str(fill_id),
                    str(broker),
                    str(symbol),
                    int(fill_ts_ms),
                    float(qty),
                    float(fill_px),
                    float(fees) if fees is not None else None,
                    json.dumps(extra or {}, separators=(",", ":"), sort_keys=True),
                ),
            )
        else:
            con.execute(
                """
                INSERT INTO execution_fills(
                  client_order_id, broker, symbol, fill_ts_ms, fill_qty, fill_px, fees, extra_json
                )
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(client_order_id),
                    str(broker),
                    str(symbol),
                    int(fill_ts_ms),
                    float(qty),
                    float(fill_px),
                    float(fees) if fees is not None else None,
                    json.dumps(extra or {}, separators=(",", ":"), sort_keys=True),
                ),
            )
        con.commit()
    finally:
        con.close()


def _vwap_for_client(con, client_order_id: str) -> Tuple[Optional[float], float, float]:
    """
    Returns (vwap, filled_qty_signed, fees_sum)

    Uses signed sum of fill_qty to preserve direction (old behavior).
    """
    rows = con.execute(
        """
        SELECT fill_qty, fill_px, COALESCE(fees,0)
        FROM execution_fills
        WHERE client_order_id=?
        """,
        (str(client_order_id),),
    ).fetchall()

    qty_sum = 0.0
    notional = 0.0
    fees = 0.0

    for q, px, f in rows or []:
        qf = float(q or 0.0)
        pxf = float(px or 0.0)
        qty_sum += qf
        notional += qf * pxf
        fees += float(f or 0.0)

    if abs(qty_sum) < 1e-12:
        return None, 0.0, float(fees)

    vwap = notional / qty_sum
    return float(vwap), float(qty_sum), float(fees)


def _last_price(con, symbol: str) -> Optional[float]:
    """
    Uses existing prices table if present (fail-soft).
    Supports both column names: px (old) and price (some deployments).
    """
    try:
        r = con.execute(
            "SELECT px FROM prices WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1",
            (str(symbol),),
        ).fetchone()
        if r and r[0] is not None:
            px = float(r[0])
            return px if px > 0 else None
    except Exception:
        pass

    try:
        r = con.execute(
            "SELECT price FROM prices WHERE symbol=? ORDER BY ts_ms DESC LIMIT 1",
            (str(symbol),),
        ).fetchone()
        if r and r[0] is not None:
            px = float(r[0])
            return px if px > 0 else None
    except Exception:
        return None

    return None


def compute_metrics_snapshot(limit_orders: int = 500) -> Dict[str, Any]:
    """
    Computes:
      - slippage_bps vs ref_px for each executed client_order_id
      - mark-to-market pnl (signed) using latest price (from prices table)
    Stores into execution_metrics at current ts_ms.
    """
    init_execution_ledger()
    con = connect()
    try:
        ts = now_ms()
        # Back-compat behavior: prevent duplicate snapshots within same second bucket
        ts = ts - (ts % 1000)

        rows = con.execute(
            """
            SELECT client_order_id, broker, symbol, qty, ref_px, submit_ts_ms
            FROM execution_orders
            ORDER BY submit_ts_ms DESC
            LIMIT ?
            """,
            (int(max(1, min(20000, int(limit_orders)))),),
        ).fetchall()

        n = 0
        for cid, broker, sym, qty, ref_px, submit_ts_ms in rows or []:
            cid = str(cid)
            broker = str(broker) if broker is not None else None
            sym = str(sym)
            qty = float(qty or 0.0)
            ref_px_f = float(ref_px) if ref_px is not None else None

            vwap, filled_qty_signed, fees = _vwap_for_client(con, cid)
            if vwap is None:
                continue

            sl_bps = None
            if ref_px_f is not None and ref_px_f > 0:
                sign = 1.0 if qty > 0 else -1.0
                sl_bps = ((float(vwap) - float(ref_px_f)) / float(ref_px_f)) * 10000.0 * sign

            last_px = _last_price(con, sym)

            m2m = None
            if last_px is not None:
                m2m = (float(last_px) - float(vwap)) * float(filled_qty_signed)

            con.execute(
                """
                INSERT OR REPLACE INTO execution_metrics(
                  ts_ms, client_order_id, broker, symbol,
                  submit_qty, filled_qty,
                  ref_px, fill_vwap, slippage_bps, fees, m2m_pnl, last_px
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts),
                    cid,
                    broker,
                    sym,
                    float(qty),
                    float(filled_qty_signed),
                    float(ref_px_f) if ref_px_f is not None else None,
                    float(vwap),
                    float(sl_bps) if sl_bps is not None else None,
                    float(fees) if fees is not None else 0.0,
                    float(m2m) if m2m is not None else None,
                    float(last_px) if last_px is not None else None,
                ),
            )
            n += 1

        con.commit()
        return {"ok": True, "metrics_written": int(n), "ts_ms": int(ts)}
    finally:
        con.close()


def compute_pnl_attribution_snapshot(lookback_orders: int = 500) -> Dict[str, Any]:
    """
    Groups execution_metrics (latest snapshot) by source_alert_id + symbol.

    Writes:
      - pnl_attribution (uses latest execution_metrics snapshot ts_ms)
      - capital_efficiency (old table; source_alert_id+symbol)
    """
    init_execution_ledger()
    con = connect()
    try:
        ts = now_ms()

        r = con.execute("SELECT MAX(ts_ms) FROM execution_metrics").fetchone()
        mts = int(r[0]) if r and r[0] is not None else None
        if mts is None:
            return {"ok": False, "status": "no_execution_metrics"}

        rows = con.execute(
            """
            SELECT o.source_alert_id, o.symbol,
                   COALESCE(m.m2m_pnl,0),
                   COALESCE(m.slippage_bps,0),
                   COALESCE(m.fees,0),
                   COALESCE(o.submit_ts_ms,0)
            FROM execution_orders o
            JOIN execution_metrics m
              ON m.client_order_id = o.client_order_id
            WHERE m.ts_ms = ?
            ORDER BY o.submit_ts_ms DESC
            LIMIT ?
            """,
            (int(mts), int(max(1, min(20000, int(lookback_orders))))),
        ).fetchall()

        agg: Dict[Tuple[int, str], Dict[str, float]] = {}
        for sid, sym, pnl, sl, fees, submit_ts_ms in rows or []:
            if sid is None:
                continue
            k = (int(sid), str(sym))
            cur = agg.get(k) or {"pnl": 0.0, "fees": 0.0, "slippage_bps": 0.0, "n": 0.0, "min_submit_ts": 0.0}
            cur["pnl"] += float(pnl or 0.0)
            cur["fees"] += float(fees or 0.0)
            cur["slippage_bps"] += float(sl or 0.0)
            cur["n"] += 1.0
            st = float(submit_ts_ms or 0.0)
            if cur["min_submit_ts"] <= 0.0 or (st > 0.0 and st < cur["min_submit_ts"]):
                cur["min_submit_ts"] = st
            agg[k] = cur

        n = 0
        for (sid, sym), v in agg.items():
            avg_sl = float(v["slippage_bps"]) / max(1.0, float(v["n"]))
            pnl = float(v["pnl"])
            fees_sum = float(v["fees"])

            # capital_hours based on first submit among grouped orders (old behavior)
            first_submit = int(v.get("min_submit_ts") or 0.0) or None
            capital_hours = 0.0
            if first_submit:
                capital_hours = max(0.0, (int(ts) - int(first_submit)) / 3600000.0)

            risk_unit = abs(pnl) if abs(pnl) > 1e-9 else 1.0
            return_per_risk = pnl / risk_unit

            drawdown = -min(0.0, pnl)

            efficiency_score = 0.0
            if capital_hours > 0:
                efficiency_score = (pnl / capital_hours)

            con.execute(
                """
                INSERT OR REPLACE INTO pnl_attribution(
                  ts_ms, source_alert_id, symbol,
                  pnl, fees, slippage_bps, extra_json
                )
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    int(mts),
                    int(sid),
                    str(sym),
                    float(pnl),
                    float(fees_sum),
                    float(avg_sl),
                    json.dumps(
                        {"metrics_ts_ms": int(mts), "n_orders": int(v["n"])},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )

            con.execute(
                """
                INSERT OR REPLACE INTO capital_efficiency(
                  ts_ms, source_alert_id, symbol,
                  capital_hours, return_per_risk,
                  drawdown_contribution, efficiency_score,
                  extra_json
                )
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(mts),
                    int(sid),
                    str(sym),
                    float(capital_hours),
                    float(return_per_risk),
                    float(drawdown),
                    float(efficiency_score),
                    json.dumps({"pnl": float(pnl), "fees": float(fees_sum)}, separators=(",", ":"), sort_keys=True),
                ),
            )

            n += 1

        con.commit()
        return {"ok": True, "attribution_written": int(n), "ts_ms": int(mts), "metrics_ts_ms": int(mts)}
    finally:
        con.close()


def _safe_json_obj(s: Optional[str]) -> Dict:
    try:
        obj = json.loads(s or "{}")
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _extract_strategy_name(extra_json: Optional[str]) -> Optional[str]:
    obj = _safe_json_obj(extra_json)

    try:
        v = obj.get("strategy_name")
        if v:
            return str(v)
    except Exception:
        pass

    try:
        ex = obj.get("explain")
        if isinstance(ex, dict):
            st = ex.get("strategy")
            if isinstance(st, dict) and st.get("name"):
                return str(st.get("name"))
    except Exception:
        pass

    try:
        st = obj.get("strategy")
        if isinstance(st, dict) and st.get("name"):
            return str(st.get("name"))
    except Exception:
        pass

    return None


def compute_capital_efficiency_snapshot(limit_orders: int = 5000) -> Dict[str, Any]:
    """
    Computes capital efficiency metrics at the latest execution_metrics snapshot:

      - capital_hours: notional * holding_hours
      - return_per_risk: pnl_net / notional
      - drawdown_contrib: min(0, pnl_net) / notional
      - efficiency_score: pnl_net / capital_hours

    Writes:
      - execution_capital_efficiency rows (order-level)
      - strategy_metrics window_days=0 (strategy-level aggregates) if available
    """
    init_execution_ledger()
    try:
        from engine.dev_core.storage import init_db

        init_db()
    except Exception:
        pass

    con = connect()
    try:
        r = con.execute("SELECT MAX(ts_ms) FROM execution_metrics").fetchone()
        mts = int(r[0]) if r and r[0] is not None else None
        if mts is None:
            return {"ok": False, "status": "no_execution_metrics"}

        rows = con.execute(
            """
            SELECT o.client_order_id, o.broker, o.portfolio_orders_id, o.source_alert_id,
                   o.symbol, o.qty, o.submit_ts_ms, o.extra_json,
                   m.fill_vwap, m.m2m_pnl, COALESCE(m.fees,0)
            FROM execution_orders o
            JOIN execution_metrics m
              ON m.client_order_id = o.client_order_id
            WHERE m.ts_ms = ?
            ORDER BY o.submit_ts_ms DESC
            LIMIT ?
            """,
            (int(mts), int(max(1, min(20000, int(limit_orders))))),
        ).fetchall()

        wrote = 0
        agg: Dict[str, Dict[str, float]] = {}

        for (
            cid,
            broker,
            portfolio_orders_id,
            source_alert_id,
            sym,
            submit_qty,
            submit_ts_ms,
            extra_json,
            _fill_vwap,
            m2m_pnl,
            fees_m,
        ) in (rows or []):
            cid = str(cid)
            sym = str(sym)
            broker_s = str(broker) if broker is not None else None

            vwap, filled_qty_signed, fees_fills = _vwap_for_client(con, cid)
            if vwap is None:
                continue

            vwap = float(vwap)
            filled_qty_signed = float(filled_qty_signed)
            fees_total = float(fees_fills or 0.0)
            if fees_m is not None:
                try:
                    fees_total = max(fees_total, float(fees_m))
                except Exception:
                    pass

            notional = abs(float(filled_qty_signed) * float(vwap))

            holding_hours = 0.0
            try:
                holding_ms = max(0, int(mts) - int(submit_ts_ms or 0))
                holding_hours = float(holding_ms) / 3_600_000.0
            except Exception:
                holding_hours = 0.0

            capital_hours = float(notional) * float(holding_hours)

            pnl_net = float(m2m_pnl or 0.0) - float(fees_total or 0.0)
            return_per_risk = float(pnl_net) / max(1e-9, float(notional))
            dd_contrib = min(0.0, float(pnl_net)) / max(1e-9, float(notional))
            efficiency_score = float(pnl_net) / max(1e-9, float(capital_hours))

            strategy_name = _extract_strategy_name(extra_json)

            con.execute(
                """
                INSERT OR REPLACE INTO execution_capital_efficiency(
                  ts_ms, client_order_id, broker, portfolio_orders_id, source_alert_id, strategy_name,
                  symbol, submit_ts_ms, filled_qty, fill_vwap, fees,
                  notional, holding_hours, capital_hours, pnl_net,
                  return_per_risk, drawdown_contrib, efficiency_score, extra_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(mts),
                    cid,
                    broker_s,
                    int(portfolio_orders_id) if portfolio_orders_id is not None else None,
                    int(source_alert_id) if source_alert_id is not None else None,
                    str(strategy_name) if strategy_name else None,
                    sym,
                    int(submit_ts_ms) if submit_ts_ms is not None else None,
                    float(filled_qty_signed),
                    float(vwap),
                    float(fees_total),
                    float(notional),
                    float(holding_hours),
                    float(capital_hours),
                    float(pnl_net),
                    float(return_per_risk),
                    float(dd_contrib),
                    float(efficiency_score),
                    extra_json,
                ),
            )
            wrote += 1

            if strategy_name:
                cur = agg.get(str(strategy_name)) or {
                    "capital_hours": 0.0,
                    "pnl_net": 0.0,
                    "notional": 0.0,
                    "dd_sum": 0.0,
                    "n": 0.0,
                }
                cur["capital_hours"] += float(capital_hours)
                cur["pnl_net"] += float(pnl_net)
                cur["notional"] += float(notional)
                cur["dd_sum"] += float(min(0.0, pnl_net))
                cur["n"] += 1.0
                agg[str(strategy_name)] = cur

        wrote_strat = 0
        for sname, v in (agg or {}).items():
            cap_h = float(v.get("capital_hours") or 0.0)
            pnl = float(v.get("pnl_net") or 0.0)
            notional_sum = float(v.get("notional") or 0.0)
            dd_sum = float(v.get("dd_sum") or 0.0)
            n_orders = int(v.get("n") or 0.0)

            metrics = {
                "ts_ms": int(mts),
                "capital_hours": float(cap_h),
                "notional": float(notional_sum),
                "pnl_net": float(pnl),
                "return_per_risk_unit": float(pnl) / max(1e-9, float(notional_sum)),
                "drawdown_contribution": float(dd_sum) / max(1e-9, float(notional_sum)),
                "efficiency_score": float(pnl) / max(1e-9, float(cap_h)),
                "n_orders": int(n_orders),
            }

            try:
                con.execute(
                    """
                    INSERT INTO strategy_metrics(strategy_name, window_days, ts_ms, metrics_json)
                    VALUES(?,?,?,?)
                    ON CONFLICT(strategy_name, window_days) DO UPDATE SET
                      ts_ms=excluded.ts_ms,
                      metrics_json=excluded.metrics_json
                    """,
                    (
                        str(sname),
                        0,
                        int(mts),
                        json.dumps(metrics, separators=(",", ":"), sort_keys=True),
                    ),
                )
                wrote_strat += 1
            except Exception:
                pass

        con.commit()
        return {
            "ok": True,
            "ts_ms": int(mts),
            "orders_written": int(wrote),
            "strategies_written": int(wrote_strat),
        }
    finally:
        con.close()
