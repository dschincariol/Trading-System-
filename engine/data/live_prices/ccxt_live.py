# dev_core/live_prices/ccxt_live.py
import time
from typing import Dict

import ccxt

def fetch_latest_ohlcv_ccxt(exchange_id: str, market_map: Dict[str, str], timeframe: str = "1m") -> Dict[str, dict]:
    """
    Returns latest OHLCV bar for each market via CCXT.
    Output:
      { "BTC": {"ts_ms":..., "tf_s":60, "o":..,"h":..,"l":..,"c":..,"v":..}, ... }
    """
    out: Dict[str, dict] = {}
    tf_s = 60 if timeframe == "1m" else 300 if timeframe == "5m" else 60

    ex_class = getattr(ccxt, exchange_id, None)
    if ex_class is None:
        return out

    ex = ex_class({"enableRateLimit": True})

    for sym, market in (market_map or {}).items():
        try:
            bars = ex.fetch_ohlcv(market, timeframe=timeframe, limit=2)
            if not bars:
                continue
            ts, o, h, l, c, v = bars[-1]
            out[str(sym)] = {
                "ts_ms": int(ts),
                "tf_s": int(tf_s),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "c": float(c),
                "v": float(v) if v is not None else None,
            }
        except Exception:
            continue

    return out


def fetch_last_prices_ccxt(exchange_id: str, market_map: Dict[str, str]) -> Dict[str, dict]:
    """
    exchange_id: "binance", "kraken", etc.
    market_map: { "BTC": "BTC/USDT", ... }
    Returns: { "BTC": {ts_ms, price}, ... }
    """
    out = {}
    now_ms = int(time.time() * 1000)

    ex_class = getattr(ccxt, exchange_id, None)
    if ex_class is None:
        return out

    ex = ex_class({"enableRateLimit": True})

    for sym, market in market_map.items():
        try:
            t = ex.fetch_ticker(market)
            last = t.get("last", None)
            if last is None:
                continue
            out[sym] = {"ts_ms": now_ms, "price": float(last)}
        except Exception:
            continue

    return out
