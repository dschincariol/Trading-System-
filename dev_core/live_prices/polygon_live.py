"""
Polygon price adapter.

Provides:
- latest price
- best-effort spread proxy (NBBO if available)
- volume + timestamp

Contract:
- fetch_last_prices(ticker_map) -> dict
"""

import os
import time
import requests


_POLYGON_KEY = os.environ.get("POLYGON_API_KEY")
_BASE = "https://api.polygon.io"


class PolygonPriceProvider:
    def __init__(self):
        if not _POLYGON_KEY:
            raise RuntimeError("POLYGON_API_KEY not set")

        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "market-impact-dev/1.0 (polygon)"}
        )

    # ------------------------------------------------------------
    # Contract: poll_prices.py
    # ------------------------------------------------------------
    def fetch_last_prices(self, ticker_map):
        """
        ticker_map: { "SPY": "SPY", ... } (values ignored; keys are symbols)

        Returns:
          {
            "SPY": {
              "ts_ms": int,
              "price": float,
              "bid": float|None,
              "ask": float|None,
              "spread": float|None,
              "volume": float|None,
              "source": "polygon"
            }
          }
        """
        out = {}

        for sym in (ticker_map or {}).keys():
            try:
                q = self.get_latest(str(sym))
                px = q.get("px")
                if px is None:
                    continue

                out[str(sym)] = {
                    "ts_ms": int(q.get("ts_ms") or int(time.time() * 1000)),
                    "price": float(px),
                    "bid": (float(q["bid"]) if q.get("bid") is not None else None),
                    "ask": (float(q["ask"]) if q.get("ask") is not None else None),
                    "spread": (float(q["spread"]) if q.get("spread") is not None else None),
                    "volume": (float(q["volume"]) if q.get("volume") is not None else None),
                    "source": "polygon",
                }
            except Exception:
                continue

        return out

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------
    def get_latest(self, symbol: str):
        """
        Returns:
          {
            px,
            ts_ms,
            bid,
            ask,
            spread,
            volume,
            source
          }
        """
        sym = symbol.upper()

        # -------------------------
        # Last trade
        # -------------------------
        r = self.session.get(
            f"{_BASE}/v2/last/trade/{sym}",
            params={"apiKey": _POLYGON_KEY},
            timeout=5,
        )
        r.raise_for_status()
        j = r.json()
        t = j.get("results", {}) or {}

        px = t.get("p")
        ts_ms = t.get("t")

        # -------------------------
        # NBBO quote (best effort)
        # -------------------------
        bid = ask = spread = None
        try:
            rq = self.session.get(
                f"{_BASE}/v2/last/nbbo/{sym}",
                params={"apiKey": _POLYGON_KEY},
                timeout=5,
            )
            rq.raise_for_status()
            q = rq.json().get("results", {}) or {}

            bid = q.get("bid_price")
            ask = q.get("ask_price")
            if bid is not None and ask is not None:
                spread = float(ask) - float(bid)
        except Exception:
            pass

        return {
            "px": px,
            "ts_ms": ts_ms or int(time.time() * 1000),
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "volume": t.get("s"),
            "source": "polygon",
        }
