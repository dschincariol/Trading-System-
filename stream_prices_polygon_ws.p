"""
Daemon: Polygon WebSocket live prices -> SQLite

Writes:
  - price_quotes_raw (per-provider)
  - price_quotes (last/bid/ask/spread/volume)
  - prices (last trade price, for downstream compatibility)

Env:
  POLYGON_API_KEY (required)
  POLYGON_WS_ENDPOINT (default: wss://socket.polygon.io/stocks)
  POLYGON_WS_SUBSCRIBE_TRADES (default: 1)
  POLYGON_WS_SUBSCRIBE_QUOTES (default: 1)

  STREAM_PRICES_FLUSH_MS (default: 250)
  STREAM_PRICES_HEARTBEAT_S (default: 2.0)
  STREAM_PRICES_MIN_WRITE_INTERVAL_MS (default: 250)

  JOB_LOCK_STALE_AFTER_S (default: 180)

Notes:
  - Subscribes to ACTIVE/WATCH symbols that have meta_json.price_provider == 'polygon'
    OR no explicit provider (defaults to polygon for equities in the early live phase).
"""

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

from engine.dev_core.storage import (
    connect,
    init_db,
    acquire_job_lock,
    release_job_lock,
    touch_job_lock,
    put_job_heartbeat,
)

JOB_NAME = "stream_prices_polygon_ws"
OWNER = os.environ.get(
    "JOB_OWNER",
    os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
)
PID = os.getpid()

LOCK_STALE_AFTER_S = int(os.environ.get("JOB_LOCK_STALE_AFTER_S", "180"))

PROVIDER_NAME = "polygon_ws"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return None


def _load_symbol_map() -> Dict[str, str]:
    """Return mapping internal symbol -> polygon ticker."""
    con = connect(readonly=True)
    try:
        rows = con.execute(
            """
            SELECT symbol, meta_json
            FROM symbols
            WHERE status IN ('ACTIVE','WATCH')
            """
        ).fetchall()
    finally:
        con.close()

    out: Dict[str, str] = {}
    for sym, meta_json in rows:
        try:
            meta = json.loads(meta_json) if meta_json else {}
        except Exception:
            meta = {}

        provider = (meta.get("price_provider") or "").lower().strip()
        poly = (meta.get("polygon_ticker") or sym)
        if provider in ("", "polygon", "polygon_ws"):
            out[str(sym)] = str(poly)

    return out


class _WsIngest:
    def __init__(self, api_key: str, endpoint: str, subscribe_trades: bool, subscribe_quotes: bool):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")

        self.api_key = api_key
        self.endpoint = endpoint
        self.sub_trades = bool(subscribe_trades)
        self.sub_quotes = bool(subscribe_quotes)

        self._lock = threading.RLock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False

        self._subscribed: Set[str] = set()

        # ticker -> latest fields
        self._last: Dict[str, Dict[str, Any]] = {}
        self._last_msg_ts_ms = 0

        self._start()

    def close(self) -> None:
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def last_msg_age_ms(self) -> int:
        ts = int(self._last_msg_ts_ms or 0)
        if ts <= 0:
            return 10**9
        return _now_ms() - ts

    def ensure_subscriptions(self, poly_tickers: Set[str]) -> None:
        poly_tickers = set([str(x) for x in (poly_tickers or set()) if str(x).strip()])
        if not poly_tickers:
            return

        to_add: Set[str] = set()
        with self._lock:
            for t in poly_tickers:
                if t not in self._subscribed:
                    to_add.add(t)

        if not to_add:
            return

        params: List[str] = []
        if self.sub_trades:
            params.extend([f"T.{t}" for t in sorted(to_add)])
        if self.sub_quotes:
            params.extend([f"Q.{t}" for t in sorted(to_add)])

        if not params:
            return

        msg = {"action": "subscribe", "params": ",".join(params)}

        try:
            if self._ws:
                self._ws.send(json.dumps(msg))
                with self._lock:
                    self._subscribed |= set(to_add)
        except Exception:
            pass

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in (self._last or {}).items()}

    # ----------------------------
    # WS lifecycle
    # ----------------------------

    def _start(self) -> None:
        t = threading.Thread(target=self._run, name="polygon_ws_ingest", daemon=True)
        self._thread = t
        t.start()

    def _run(self) -> None:
        backoff_s = 1.0
        while not self._stop:
            try:
                self._ws = websocket.WebSocketApp(
                    self.endpoint,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=25, ping_timeout=10, origin=None)
            except Exception:
                pass

            if self._stop:
                break

            time.sleep(min(30.0, max(1.0, backoff_s)))
            backoff_s = min(30.0, backoff_s * 1.8)

    def _on_open(self, ws):
        try:
            ws.send(json.dumps({"action": "auth", "params": self.api_key}))
        except Exception:
            pass

    def _on_close(self, ws, code=None, msg=None):
        return

    def _on_error(self, ws, error):
        return

    def _on_message(self, ws, message: str):
        now_ms = _now_ms()
        self._last_msg_ts_ms = now_ms

        payload = _safe_json_loads(message)
        if payload is None:
            return

        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return

        with self._lock:
            for ev in payload:
                if not isinstance(ev, dict):
                    continue

                et = str(ev.get("ev") or "")
                if et == "status":
                    continue

                sym = ev.get("sym") or ev.get("symbol")
                if not sym:
                    continue
                sym = str(sym)

                rec = self._last.get(sym) or {"ts_ms": now_ms}

                if et == "T":
                    p = ev.get("p")
                    if p is not None:
                        try:
                            rec["last"] = float(p)
                        except Exception:
                            pass
                    sz = ev.get("s")
                    if sz is not None:
                        try:
                            rec["volume"] = float(sz)
                        except Exception:
                            pass
                    tms = ev.get("t")
                    if tms is not None:
                        try:
                            rec["ts_ms"] = int(tms)
                        except Exception:
                            rec["ts_ms"] = now_ms
                    else:
                        rec["ts_ms"] = now_ms

                elif et == "Q":
                    bp = ev.get("bp")
                    ap = ev.get("ap")
                    if bp is not None:
                        try:
                            rec["bid"] = float(bp)
                        except Exception:
                            pass
                    if ap is not None:
                        try:
                            rec["ask"] = float(ap)
                        except Exception:
                            pass

                    if ("bid" in rec) and ("ask" in rec):
                        try:
                            rec["spread"] = float(rec["ask"]) - float(rec["bid"])
                        except Exception:
                            pass

                    tms = ev.get("t")
                    if tms is not None:
                        try:
                            rec["ts_ms"] = int(tms)
                        except Exception:
                            rec["ts_ms"] = now_ms
                    else:
                        rec["ts_ms"] = now_ms

                else:
                    continue

                self._last[sym] = rec


def _flush_to_db(
    con,
    ts_ms: int,
    sym_to_poly: Dict[str, str],
    poly_snapshot: Dict[str, Dict[str, Any]],
    min_write_interval_ms: int,
    last_write_by_symbol: Dict[str, int],
) -> Tuple[int, int, int]:
    n_raw = 0
    n_q = 0
    n_px = 0

    raw_rows: List[tuple] = []
    q_rows: List[tuple] = []
    px_rows: List[tuple] = []

    for sym, poly in (sym_to_poly or {}).items():
        rec = poly_snapshot.get(str(poly))
        if not rec:
            continue

        rts = int(rec.get("ts_ms") or ts_ms)
        if rts <= 0:
            rts = ts_ms

        last_ts = int(last_write_by_symbol.get(sym) or 0)
        if (rts - last_ts) < int(min_write_interval_ms):
            continue

        last_write_by_symbol[sym] = rts

        last = rec.get("last")
        bid = rec.get("bid")
        ask = rec.get("ask")
        spread = rec.get("spread")
        vol = rec.get("volume")

        try:
            last_f = float(last) if last is not None else None
        except Exception:
            last_f = None
        try:
            bid_f = float(bid) if bid is not None else None
        except Exception:
            bid_f = None
        try:
            ask_f = float(ask) if ask is not None else None
        except Exception:
            ask_f = None
        try:
            spread_f = float(spread) if spread is not None else (float(ask_f) - float(bid_f) if (ask_f is not None and bid_f is not None) else None)
        except Exception:
            spread_f = None
        try:
            vol_f = float(vol) if vol is not None else None
        except Exception:
            vol_f = None

        raw_rows.append((int(rts), str(sym), str(PROVIDER_NAME), last_f, bid_f, ask_f, spread_f, vol_f))
        q_rows.append((int(rts), str(sym), last_f, bid_f, ask_f, spread_f, vol_f, str(PROVIDER_NAME)))

        if last_f is not None:
            px_rows.append((int(rts), str(sym), float(last_f), str(PROVIDER_NAME)))

    if raw_rows:
        con.executemany(
            """
            INSERT OR REPLACE INTO price_quotes_raw(
              ts_ms, symbol, provider,
              last, bid, ask, spread, volume
            )
            VALUES (?,?,?,?,?,?,?,?)
            """,
            raw_rows,
        )
        n_raw = len(raw_rows)

    if q_rows:
        con.executemany(
            """
            INSERT OR REPLACE INTO price_quotes(
              ts_ms, symbol,
              last, bid, ask, spread, volume,
              source
            )
            VALUES (?,?,?,?,?,?,?,?)
            """,
            q_rows,
        )
        n_q = len(q_rows)

    if px_rows:
        con.executemany(
            """
            INSERT OR REPLACE INTO prices(ts_ms, symbol, px, source)
            VALUES (?,?,?,?)
            """,
            px_rows,
        )
        n_px = len(px_rows)

    return n_raw, n_q, n_px


def main():
    init_db()

    if not acquire_job_lock(JOB_NAME, OWNER, PID, ttl_s=LOCK_STALE_AFTER_S):
        raise SystemExit(2)

    api_key = os.environ.get("POLYGON_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("POLYGON_API_KEY not set")

    endpoint = os.environ.get("POLYGON_WS_ENDPOINT", "wss://socket.polygon.io/stocks").strip()
    sub_trades = os.environ.get("POLYGON_WS_SUBSCRIBE_TRADES", "1") == "1"
    sub_quotes = os.environ.get("POLYGON_WS_SUBSCRIBE_QUOTES", "1") == "1"

    flush_ms = int(os.environ.get("STREAM_PRICES_FLUSH_MS", "250"))
    hb_s = float(os.environ.get("STREAM_PRICES_HEARTBEAT_S", "2.0"))
    min_write_ms = int(os.environ.get("STREAM_PRICES_MIN_WRITE_INTERVAL_MS", str(flush_ms)))

    ws = _WsIngest(api_key=api_key, endpoint=endpoint, subscribe_trades=sub_trades, subscribe_quotes=sub_quotes)

    last_hb = 0.0
    last_sym_reload_ms = 0
    sym_to_poly: Dict[str, str] = {}
    last_write_by_symbol: Dict[str, int] = {}

    try:
        while True:
            now_s = time.time()
            now_ms = _now_ms()

            if (now_ms - last_sym_reload_ms) >= 30_000 or not sym_to_poly:
                sym_to_poly = _load_symbol_map()
                ws.ensure_subscriptions(set(sym_to_poly.values()))
                last_sym_reload_ms = now_ms

            if (now_s - last_hb) >= hb_s:
                touch_job_lock(JOB_NAME, OWNER, PID)
                put_job_heartbeat(
                    JOB_NAME,
                    OWNER,
                    PID,
                    extra_json=json.dumps(
                        {
                            "provider": PROVIDER_NAME,
                            "ws_age_ms": int(ws.last_msg_age_ms()),
                            "n_symbols": int(len(sym_to_poly)),
                        },
                        separators=(",", ":"),
                    ),
                )
                last_hb = now_s

            snap = ws.snapshot()

            con = connect(readonly=False)
            try:
                con.execute("BEGIN IMMEDIATE;")
                _flush_to_db(
                    con,
                    ts_ms=now_ms,
                    sym_to_poly=sym_to_poly,
                    poly_snapshot=snap,
                    min_write_interval_ms=min_write_ms,
                    last_write_by_symbol=last_write_by_symbol,
                )
                con.execute("COMMIT;")
            except Exception:
                try:
                    con.execute("ROLLBACK;")
                except Exception:
                    pass
            finally:
                con.close()

            time.sleep(max(0.05, float(flush_ms) / 1000.0))

    finally:
        try:
            ws.close()
        except Exception:
            pass
        release_job_lock(JOB_NAME, OWNER, PID)


if __name__ == "__main__":
    main()
