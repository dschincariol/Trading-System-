# engine/api/api_market.py
"""Market data endpoints (candles + live stream).

Provides:
  - GET /api/market/candles?symbol=SPY&tf=1m&limit=500
  - GET /api/market/stream?symbol=SPY&tf=1m      (SSE)

Data source:
  - price_quotes_raw where provider='polygon_ws'
    (written by engine/data/stream_prices_polygon_ws.py)
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from engine.api.http_parsing import qs as _qs
from engine.api.http_transport import StreamingResponse

from engine.runtime.storage import connect_ro


ROUTE_SPECS_MARKET = [
    ("GET", "/api/market/candles", "api_get_market_candles"),
    ("GET", "/api/market/stream", "api_get_market_stream"),
]


def _tf_to_ms(tf: str) -> int:
    t = (tf or "").strip().lower()
    if t in ("1m", "1min", "60s"):
        return 60_000
    if t in ("5m", "5min"):
        return 5 * 60_000
    if t in ("15m", "15min"):
        return 15 * 60_000
    if t in ("30m", "30min"):
        return 30 * 60_000
    if t in ("1h", "60m"):
        return 60 * 60_000
    if t in ("4h", "240m"):
        return 4 * 60 * 60_000
    if t in ("1d", "day"):
        return 24 * 60 * 60_000
    return 60_000


def _bucket_ms(ts_ms: int, tf_ms: int) -> int:
    if tf_ms <= 0:
        tf_ms = 60_000
    return (int(ts_ms) // int(tf_ms)) * int(tf_ms)


def _rows_since(
    con,
    *,
    symbol: str,
    since_ts_ms: int,
    limit: int,
) -> List[Tuple[int, Optional[float], Optional[float]]]:
    """Returns [(ts_ms, last, volume), ...]"""
    cur = con.execute(
        """
        SELECT ts_ms, last, volume
          FROM price_quotes_raw
         WHERE symbol=?
           AND provider='polygon_ws'
           AND ts_ms > ?
         ORDER BY ts_ms ASC
         LIMIT ?
        """,
        (str(symbol), int(since_ts_ms), int(limit)),
    )
    out = []
    for r in cur.fetchall() or []:
        try:
            out.append((int(r[0]), (float(r[1]) if r[1] is not None else None), (float(r[2]) if r[2] is not None else None)))
        except Exception:
            try:
                out.append((int(r[0]), None, None))
            except Exception:
                continue
    return out


def _build_candles_from_rows(
    rows: List[Tuple[int, Optional[float], Optional[float]]],
    *,
    tf_ms: int,
) -> List[Dict[str, Any]]:
    """Aggregate tick snapshots into OHLCV candles."""
    candles: List[Dict[str, Any]] = []

    cur_b = None
    cur = None

    for (ts_ms, last, vol) in rows:
        if last is None:
            continue

        b = _bucket_ms(ts_ms, tf_ms)

        if cur is None or cur_b != b:
            if cur is not None:
                candles.append(cur)
            cur_b = b
            cur = {
                "t": int(b // 1000),
                "o": float(last),
                "h": float(last),
                "l": float(last),
                "c": float(last),
                "v": float(vol or 0.0),
            }
            continue

        # update existing
        p = float(last)
        cur["c"] = p
        if p > float(cur["h"]):
            cur["h"] = p
        if p < float(cur["l"]):
            cur["l"] = p

        if vol is not None:
            try:
                cur["v"] = float(cur.get("v") or 0.0) + float(vol)
            except Exception:
                pass

    if cur is not None:
        candles.append(cur)

    return candles


def api_get_market_candles(parsed: Any, _ctx=None) -> Dict[str, Any]:
    q = _qs(parsed)
    symbol = (q.get("symbol") or "").strip().upper()
    tf = (q.get("tf") or "1m").strip()
    limit_s = (q.get("limit") or "500").strip()
    max_points_s = (q.get("max_points") or "").strip()

    if not symbol:
        return {"ok": False, "error": "missing_symbol"}

    try:
        limit = max(10, min(5000, int(limit_s)))
    except Exception:
        limit = 500

    try:
        max_points = int(max_points_s) if max_points_s else limit
        max_points = max(50, min(20000, max_points))
    except Exception:
        max_points = limit

    tf_ms = _tf_to_ms(tf)

    # Read a window back; raw snapshots may be frequent.
    # We oversample by ~10x and then aggregate.
    fetch_limit = max(200, min(50_000, limit * 10))
    now_ms = int(time.time() * 1000)
    # conservative: request last N hours for 1m candles
    lookback_ms = max(tf_ms * fetch_limit, 6 * 60 * 60_000)
    since = max(0, now_ms - lookback_ms)

    con = connect_ro()
    rows = _rows_since(con, symbol=symbol, since_ts_ms=since, limit=fetch_limit)
    candles = _build_candles_from_rows(rows, tf_ms=tf_ms)
    if len(candles) > limit:
        candles = candles[-limit:]

    # ------------------------------------------------------------
    # DECIMATE (client-safe) — cap max points for long history
    # ------------------------------------------------------------
    if max_points and len(candles) > max_points:
        step = max(1, int(len(candles) / max_points))
        candles = candles[::step]
        if candles and candles[-1] != (candles[-1]):
            candles = candles[:max_points]

    return {
        "ok": True,
        "symbol": symbol,
        "tf": tf,
        "candles": candles,
    }


def api_get_market_stream(parsed: Any, _ctx=None) -> StreamingResponse:
    q = _qs(parsed)
    symbol = (q.get("symbol") or "").strip().upper()
    tf = (q.get("tf") or "1m").strip()
    if not symbol:
        # streaming response must still be SSE compatible
        def _err_stream(handler):
            try:
                handler.wfile.write(b"event: error\n")
                handler.wfile.write(b"data: {\"ok\":false,\"error\":\"missing_symbol\"}\n\n")
                handler.wfile.flush()
            except Exception:
                pass

        return StreamingResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-store",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
            },
            stream_fn=_err_stream,
        )

    tf_ms = _tf_to_ms(tf)

    def _stream(handler):
        # Initial hello
        try:
            handler.wfile.write(b"event: hello\n")
            handler.wfile.write(
                ("data: " + json.dumps({"ok": True, "symbol": symbol, "tf": tf}) + "\n\n").encode("utf-8")
            )
            handler.wfile.flush()
        except Exception:
            return

        con = None
        last_ts = 0

        # Candle accumulator
        cur_b = None
        cur = None

        # Emit control
        last_emit_ms = 0
        last_sent_json = None

        # Keepalive control
        last_ping_ms = 0
        PING_EVERY_MS = 15_000

        while True:
            try:
                if con is None:
                    con = connect_ro()

                rows = _rows_since(con, symbol=symbol, since_ts_ms=last_ts, limit=2000)
                if rows:
                    last_ts = int(rows[-1][0] or last_ts)

                if rows:
                    for (ts_ms, last, vol) in rows:
                        if last is None:
                            continue

                        b = _bucket_ms(ts_ms, tf_ms)
                        p = float(last)
                        v = float(vol or 0.0)

                        if cur is None or cur_b != b:
                            cur_b = b
                            cur = {
                                "t": int(b // 1000),
                                "o": p,
                                "h": p,
                                "l": p,
                                "c": p,
                                "v": v,
                            }
                        else:
                            cur["c"] = p
                            if p > float(cur["h"]):
                                cur["h"] = p
                            if p < float(cur["l"]):
                                cur["l"] = p
                            cur["v"] = float(cur.get("v") or 0.0) + v

                    now_ms = int(time.time() * 1000)
                    if cur is not None and (now_ms - last_emit_ms) >= 250:
                        payload = json.dumps(cur, separators=(",", ":"), sort_keys=True)
                        if payload != last_sent_json:
                            last_sent_json = payload
                            last_emit_ms = now_ms
                            try:
                                handler.wfile.write(b"event: candle\n")
                                handler.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                                handler.wfile.flush()
                            except Exception:
                                return

                now_ms = int(time.time() * 1000)
                if (now_ms - last_ping_ms) >= PING_EVERY_MS:
                    last_ping_ms = now_ms
                    try:
                        handler.wfile.write(b": ping\n\n")
                        handler.wfile.flush()
                    except Exception:
                        return

                time.sleep(0.5)

            except Exception:
                try:
                    handler.wfile.write(b"event: error\n")
                    handler.wfile.write(b"data: {\"ok\":false,\"error\":\"stream_failed\"}\n\n")
                    handler.wfile.flush()
                except Exception:
                    return
                time.sleep(1.0)

            except Exception:
                try:
                    handler.wfile.write(b"event: error\n")
                    handler.wfile.write(b"data: {\"ok\":false,\"error\":\"stream_failed\"}\n\n")
                    handler.wfile.flush()
                except Exception:
                    return
                time.sleep(1.0)

    return StreamingResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        },
        stream_fn=_stream,
    )