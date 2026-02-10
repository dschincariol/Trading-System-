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
from typing import Any, Dict, List, Optional, Tuple

from dev_core.storage import connect


SCHEMA = """
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

CREATE TABLE IF NOT EXISTS execution_fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  client_order_id TEXT NOT NULL,
  fill_ts_ms INTEGER NOT NULL,
  fill_qty REAL NOT NULL,
  fill_px REAL NOT NULL,
  fees REAL,
  liquidity TEXT,
  raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_fills_client
  ON execution_fills(client_order_id);

CREATE TABLE IF NOT EXISTS execution_metrics (
  ts_ms INTEGER NOT NULL,
  client_order_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  ref_px REAL,
  fill_vwap REAL,
  slippage_bps REAL,
  m2m_pnl REAL,
  last_px REAL,
  PRIMARY KEY (ts_ms, client_order_id)
);

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


def log_fill(
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
              client_order_id, fill_ts_ms, fill_qty, fill_px, fees, liquidity, raw_json
            )
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                str(client_order_id),
                int(fill_ts_ms),
                float(fill_qty),
                float(fill_px),
                float(fees) if fees is not None else None,
                str(liquidity) if liquidity is not None else None,
                json.dumps(raw or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()


def _vwap_for_client(con, client_order_id: str) -> Tuple[Optional[float], float, float]:
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
        q = float(q or 0.0)
        px = float(px or 0.0)
        qty_sum += q
        notional += q * px
        fees += float(f or 0.0)

    if abs(qty_sum) < 1e-12:
        return None, 0.0, fees

    vwap = notional / qty_sum
    return float(vwap), float(qty_sum), float(fees)


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
        # Prevent duplicate snapshots within same ms
        ts = ts - (ts % 1000)

        rows = con.execute(
            """
            SELECT client_order_id, symbol, qty, ref_px, submit_ts_ms
            FROM execution_orders
            ORDER BY submit_ts_ms DESC
            LIMIT ?
            """,
            (int(max(1, min(5000, int(limit_orders)))),),
        ).fetchall()

        n = 0
        for cid, sym, qty, ref_px, submit_ts_ms in rows or []:
            cid = str(cid)
            sym = str(sym)
            qty = float(qty or 0.0)
            ref_px = float(ref_px) if ref_px is not None else None

            vwap, filled_qty, _fees = _vwap_for_client(con, cid)
            if vwap is None:
                continue

            sl_bps = None
            if ref_px and ref_px > 0:
                sign = 1.0 if qty > 0 else -1.0
                sl_bps = ((float(vwap) - float(ref_px)) / float(ref_px)) * 10000.0 * sign

            last_px = None
            try:
                r = con.execute(
                    """
                    SELECT px
                    FROM prices
                    WHERE symbol=?
                    ORDER BY ts_ms DESC
                    LIMIT 1
                    """,
                    (sym,),
                ).fetchone()
                if r:
                    last_px = float(r[0])
            except Exception:
                last_px = None

            m2m = None
            if last_px is not None:
                m2m = (float(last_px) - float(vwap)) * float(filled_qty)

            con.execute(
                """
                INSERT OR REPLACE INTO execution_metrics(
                  ts_ms, client_order_id, symbol, ref_px, fill_vwap, slippage_bps, m2m_pnl, last_px
                )
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts),
                    cid,
                    sym,
                    ref_px,
                    float(vwap),
                    float(sl_bps) if sl_bps is not None else None,
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
    """
    init_execution_ledger()
    con = connect()
    try:
        ts = now_ms()

        # Determine latest metric ts_ms available (or use current if none).
        r = con.execute("SELECT MAX(ts_ms) FROM execution_metrics").fetchone()
        mts = int(r[0]) if r and r[0] is not None else None
        if mts is None:
            return {"ok": False, "status": "no_execution_metrics"}

        rows = con.execute(
            """
            SELECT o.source_alert_id, o.symbol,
                   COALESCE(m.m2m_pnl,0), COALESCE(m.slippage_bps,0)
            FROM execution_orders o
            JOIN execution_metrics m
              ON m.client_order_id = o.client_order_id
            WHERE m.ts_ms = ?
            ORDER BY o.submit_ts_ms DESC
            LIMIT ?
            """,
            (int(mts), int(max(1, min(5000, int(lookback_orders))))),
        ).fetchall()

        agg: Dict[tuple, Dict[str, float]] = {}
        for sid, sym, pnl, sl in rows or []:
            if sid is None:
                continue
            k = (int(sid), str(sym))
            cur = agg.get(k) or {"pnl": 0.0, "slippage_bps": 0.0, "n": 0.0}
            cur["pnl"] += float(pnl or 0.0)
            cur["slippage_bps"] += float(sl or 0.0)
            cur["n"] += 1.0
            agg[k] = cur

        n = 0
        for (sid, sym), v in agg.items():
            avg_sl = float(v["slippage_bps"]) / max(1.0, float(v["n"]))

            con.execute(
                """
                INSERT OR REPLACE INTO pnl_attribution(
                  ts_ms, source_alert_id, symbol,
                  pnl, fees, slippage_bps, extra_json
                )
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    int(ts),
                    int(sid),
                    str(sym),
                    float(v["pnl"]),
                    0.0,
                    float(avg_sl),
                    json.dumps(
                        {
                            "metrics_ts_ms": int(mts),
                            "n_orders": int(v["n"]),
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
            n += 1

        con.commit()
        return {"ok": True, "attribution_written": int(n), "ts_ms": int(ts), "metrics_ts_ms": int(mts)}
    finally:
        con.close()
