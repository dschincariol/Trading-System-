# engine/api/api_terminal.py
"""
Trading Terminal API (read-mostly).

Provides:
  - GET /api/terminal/watchlist
  - GET /api/terminal/snapshot
  - GET /api/terminal/positions
  - GET /api/terminal/orders
  - GET /api/terminal/fills
  - GET /api/terminal/equity
  - GET /api/terminal/markers?symbol=SPY

All endpoints are defensive:
  - If tables are missing, returns ok:true with empty arrays so UI never crashes.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from engine.api.http_parsing import qs as _qs
from engine.runtime.storage import connect_ro


ROUTE_SPECS_TERMINAL = [
    ("GET", "/api/terminal/watchlist", "api_get_terminal_watchlist"),
    ("GET", "/api/terminal/snapshot",  "api_get_terminal_snapshot"),
    ("GET", "/api/terminal/positions", "api_get_terminal_positions"),
    ("GET", "/api/terminal/orders",    "api_get_terminal_orders"),
    ("GET", "/api/terminal/fills",     "api_get_terminal_fills"),
    ("GET", "/api/terminal/equity",    "api_get_terminal_equity"),
    ("GET", "/api/terminal/markers",   "api_get_terminal_markers"),
]


def _table_exists(con, name: str) -> bool:
    try:
        r = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def _rows_to_dicts(rows) -> List[Dict[str, Any]]:
    out = []
    if not rows:
        return out
    for r in rows:
        try:
            # sqlite3.Row supports dict(r)
            out.append(dict(r))
        except Exception:
            try:
                # fallback: mapping-ish
                out.append({k: r[k] for k in r.keys()})
            except Exception:
                out.append({"raw": str(r)})
    return out


def api_get_terminal_watchlist(_parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()

    # Prefer symbols with freshest market data
    symbols: List[str] = []

    if _table_exists(con, "price_bars"):
        try:
            rows = con.execute(
                """
                SELECT symbol, MAX(ts_ms) AS last_ts
                  FROM price_bars
                 GROUP BY symbol
                 ORDER BY last_ts DESC
                 LIMIT 200
                """
            ).fetchall()
            for r in rows or []:
                try:
                    s = str(r[0] or "").strip().upper()
                    if s and s not in symbols:
                        symbols.append(s)
                except Exception:
                    pass
        except Exception:
            pass

    # Fallback: portfolio_state symbols
    if not symbols and _table_exists(con, "portfolio_state"):
        try:
            rows = con.execute(
                """
                SELECT DISTINCT symbol
                  FROM portfolio_state
                 WHERE symbol IS NOT NULL AND symbol != ''
                 ORDER BY updated_ts_ms DESC
                 LIMIT 200
                """
            ).fetchall()
            for r in rows or []:
                try:
                    s = str(r[0] or "").strip().upper()
                    if s and s not in symbols:
                        symbols.append(s)
                except Exception:
                    pass
        except Exception:
            pass

    return {"ok": True, "symbols": symbols}


def api_get_terminal_positions(_parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()
    rows = []

    if _table_exists(con, "broker_positions"):
        try:
            rows = con.execute(
                """
                SELECT symbol, qty, avg_px, updated_ts_ms
                  FROM broker_positions
                 ORDER BY updated_ts_ms DESC
                 LIMIT 500
                """
            ).fetchall()
        except Exception:
            rows = []

    return {"ok": True, "rows": _rows_to_dicts(rows)}


def api_get_terminal_orders(parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()
    q = _qs(parsed)
    limit_s = (q.get("limit") or "500").strip()
    try:
        limit = max(1, min(5000, int(limit_s)))
    except Exception:
        limit = 500

    # Merge: broker_order_state + portfolio_orders (best effort)
    out = {"broker": [], "portfolio": []}

    if _table_exists(con, "broker_order_state"):
        try:
            rows = con.execute(
                """
                SELECT id, source_order_id, symbol, state, created_ts_ms, updated_ts_ms, ttl_ms, meta_json
                  FROM broker_order_state
                 ORDER BY updated_ts_ms DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            out["broker"] = _rows_to_dicts(rows)
        except Exception:
            out["broker"] = []

    if _table_exists(con, "portfolio_orders"):
        try:
            rows = con.execute(
                """
                SELECT id, ts_ms, symbol, action, from_side, to_side,
                       from_weight, to_weight, delta_weight, source_alert_id
                  FROM portfolio_orders
                 ORDER BY ts_ms DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            out["portfolio"] = _rows_to_dicts(rows)
        except Exception:
            out["portfolio"] = []

    return {"ok": True, "data": out}


def api_get_terminal_fills(parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()
    q = _qs(parsed)
    limit_s = (q.get("limit") or "1000").strip()
    symbol = (q.get("symbol") or "").strip().upper()

    try:
        limit = max(1, min(20000, int(limit_s)))
    except Exception:
        limit = 1000

    rows = []
    if _table_exists(con, "broker_fills"):
        try:
            if symbol:
                rows = con.execute(
                    """
                    SELECT id, ts_ms, symbol, qty, px, source_order_id, note, explain_json
                      FROM broker_fills
                     WHERE symbol=?
                     ORDER BY ts_ms DESC
                     LIMIT ?
                    """,
                    (str(symbol), int(limit)),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT id, ts_ms, symbol, qty, px, source_order_id, note, explain_json
                      FROM broker_fills
                     ORDER BY ts_ms DESC
                     LIMIT ?
                    """,
                    (int(limit),),
                ).fetchall()
        except Exception:
            rows = []

    return {"ok": True, "rows": _rows_to_dicts(rows)}


def api_get_terminal_equity(parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()
    q = _qs(parsed)

    limit_s = (q.get("limit") or "2000").strip()
    try:
        limit = max(10, min(50000, int(limit_s)))
    except Exception:
        limit = 2000

    account = None
    series = []

    if _table_exists(con, "broker_account"):
        try:
            r = con.execute(
                "SELECT cash, equity, updated_ts_ms FROM broker_account ORDER BY updated_ts_ms DESC LIMIT 1"
            ).fetchone()
            if r:
                account = {"cash": float(r[0] or 0.0), "equity": float(r[1] or 0.0), "updated_ts_ms": int(r[2] or 0)}
        except Exception:
            account = None

    if _table_exists(con, "equity_history"):
        try:
            rows = con.execute(
                """
                SELECT ts_ms, equity
                  FROM equity_history
                 ORDER BY ts_ms DESC
                 LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            # return ascending for chart
            rows = list(reversed(rows or []))
            for r in rows:
                try:
                    series.append({"t": int(int(r[0]) // 1000), "v": float(r[1] or 0.0)})
                except Exception:
                    pass
        except Exception:
            series = []

    return {"ok": True, "account": account, "series": series}


def api_get_terminal_markers(parsed: Any, _ctx=None) -> Dict[str, Any]:
    con = connect_ro()
    q = _qs(parsed)

    symbol = (q.get("symbol") or "").strip().upper()
    if not symbol:
        return {"ok": False, "error": "missing_symbol"}

    # Markers = fills + portfolio_orders
    markers: List[Dict[str, Any]] = []

    if _table_exists(con, "broker_fills"):
        try:
            rows = con.execute(
                """
                SELECT ts_ms, qty, px
                  FROM broker_fills
                 WHERE symbol=?
                 ORDER BY ts_ms DESC
                 LIMIT 2000
                """,
                (str(symbol),),
            ).fetchall()
            for r in rows or []:
                try:
                    ts = int(int(r[0]) // 1000)
                    qty = float(r[1] or 0.0)
                    px = float(r[2] or 0.0)
                    side = "BUY" if qty > 0 else "SELL"
                    markers.append({
                        "t": ts,
                        "kind": "fill",
                        "side": side,
                        "qty": qty,
                        "px": px,
                        "text": side,
                    })
                except Exception:
                    pass
        except Exception:
            pass

    if _table_exists(con, "portfolio_orders"):
        try:
            rows = con.execute(
                """
                SELECT ts_ms, action, from_side, to_side, delta_weight, source_alert_id
                  FROM portfolio_orders
                 WHERE symbol=?
                 ORDER BY ts_ms DESC
                 LIMIT 2000
                """,
                (str(symbol),),
            ).fetchall()
            for r in rows or []:
                try:
                    ts = int(int(r[0]) // 1000)
                    action = str(r[1] or "")
                    to_side = str(r[3] or "")
                    dw = float(r[4] or 0.0)
                    sid = r[5]
                    markers.append({
                        "t": ts,
                        "kind": "intent",
                        "side": (to_side or action or "INTENT").upper(),
                        "delta_weight": dw,
                        "source_alert_id": sid,
                        "text": "INTENT",
                    })
                except Exception:
                    pass
        except Exception:
            pass

    # sort ascending by time for charts that expect stable order
    try:
        markers.sort(key=lambda m: int(m.get("t") or 0))
    except Exception:
        pass

    return {"ok": True, "symbol": symbol, "markers": markers}


def api_get_terminal_snapshot(parsed: Any, _ctx=None) -> Dict[str, Any]:
    # Single call for terminal boot
    t0 = int(time.time() * 1000)

    watch = api_get_terminal_watchlist(parsed, _ctx)
    pos = api_get_terminal_positions(parsed, _ctx)
    ords = api_get_terminal_orders(parsed, _ctx)
    fills = api_get_terminal_fills(parsed, _ctx)
    eq = api_get_terminal_equity(parsed, _ctx)

    return {
        "ok": True,
        "ts_ms": int(time.time() * 1000),
        "latency_ms": int(time.time() * 1000) - t0,
        "watchlist": watch.get("symbols") if isinstance(watch, dict) else [],
        "positions": (pos.get("rows") if isinstance(pos, dict) else []),
        "orders": (ords.get("data") if isinstance(ords, dict) else {"broker": [], "portfolio": []}),
        "fills": (fills.get("rows") if isinstance(fills, dict) else []),
        "equity": {"account": eq.get("account"), "series": eq.get("series")} if isinstance(eq, dict) else {"account": None, "series": []},
    }