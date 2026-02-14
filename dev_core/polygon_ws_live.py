import json
import os
import threading
import time
from typing import Any, Dict, Optional, Set

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None


class PolygonWsPriceProvider:
    """Polygon WebSocket provider.

    Maintains one persistent websocket connection and caches the latest trade/quote
    per symbol. poll_prices.py can call fetch_last_prices() to obtain the newest
    cached data without per-symbol REST calls.

    Env:
      POLYGON_API_KEY
      POLYGON_WS_ENDPOINT (default: wss://socket.polygon.io/stocks)
      POLYGON_WS_SUBSCRIBE_TRADES (default: 1)
      POLYGON_WS_SUBSCRIBE_QUOTES (default: 1)
    """

    def __init__(self):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed")

        self.api_key = os.environ.get("POLYGON_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError("POLYGON_API_KEY not set")

        self.endpoint = os.environ.get("POLYGON_WS_ENDPOINT", "wss://socket.polygon.io/stocks").strip()
        self.sub_trades = os.environ.get("POLYGON_WS_SUBSCRIBE_TRADES", "1") == "1"
        self.sub_quotes = os.environ.get("POLYGON_WS_SUBSCRIBE_QUOTES", "1") == "1"

        self._lock = threading.RLock()
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = False

        self._subscribed: Set[str] = set()  # polygon tickers (e.g. AAPL)
        self._last: Dict[str, Dict[str, Any]] = {}  # symbol -> {last,bid,ask,volume,ts_ms}
        self._last_msg_ts_ms = 0

        self._start()

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def fetch_last_prices(self, tickers: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
        """Return cached latest prices.

        tickers: mapping of internal symbol -> polygon ticker
        """
        self._ensure_subscriptions(tickers)

        out: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            for sym, poly_ticker in (tickers or {}).items():
                if not poly_ticker:
                    continue
                rec = self._last.get(str(poly_ticker))
                if not rec:
                    continue
                out[str(sym)] = dict(rec)
        return out

    def close(self) -> None:
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------

    def _start(self) -> None:
        t = threading.Thread(target=self._run, name="polygon_ws", daemon=True)
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

                # ping/pong helps keep the socket alive across proxies
                self._ws.run_forever(
                    ping_interval=25,
                    ping_timeout=10,
                    origin=None,
                )

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

    def _ensure_subscriptions(self, tickers: Dict[str, str]) -> None:
        if not tickers:
            return

        want: Set[str] = set()
        for _sym, poly in tickers.items():
            if poly:
                want.add(str(poly))

        if not want:
            return

        to_add: Set[str] = set()
        with self._lock:
            for t in want:
                if t not in self._subscribed:
                    to_add.add(t)

        if not to_add:
            return

        params: list[str] = []
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

    def _on_message(self, ws, message: str):
        now_ms = int(time.time() * 1000)
        self._last_msg_ts_ms = now_ms

        payload = None
        try:
            payload = json.loads(message)
        except Exception:
            return

        # Polygon sends a list of events; auth/status can also come in list form.
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return

        with self._lock:
            for ev in payload:
                if not isinstance(ev, dict):
                    continue

                # status messages: {"ev":"status","message":"authenticated",...}
                et = str(ev.get("ev") or "")
                if et == "status":
                    continue

                sym = ev.get("sym") or ev.get("symbol")
                if not sym:
                    continue
                sym = str(sym)

                rec = self._last.get(sym) or {"ts_ms": now_ms}

                # Trade: ev == "T" fields commonly include p (price), s (size), t (ts)
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

                # Quote: ev == "Q" fields commonly include bp/ap (bid/ask), bs/as (sizes), t (ts)
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
