# dev_core/live_prices/yfinance_live.py
import time
from typing import Dict

import yfinance as yf

def fetch_last_prices_yf(ticker_map: Dict[str, str]) -> Dict[str, dict]:
    """
    Contract-compatible with poll_prices.py
    Returns:
      { "SPY": {"ts_ms":..., "price":..., "source":"yfinance"}, ... }
    """
    out: Dict[str, dict] = {}
    if not ticker_map:
        return out

    now_ms = int(time.time() * 1000)
    tickers = list(ticker_map.values())

    try:
        df = yf.download(
            tickers=" ".join(tickers),
            period="1d",
            interval="1m",
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=False,
        )
    except Exception:
        return out

    for sym, tkr in ticker_map.items():
        try:
            if len(tickers) == 1:
                series = df["Close"]
            else:
                series = df[tkr]["Close"]

            if series is None or series.empty:
                continue

            px = float(series.iloc[-1])
            out[str(sym)] = {
                "ts_ms": now_ms,
                "price": px,
                "bid": None,
                "ask": None,
                "spread": None,
                "volume": None,
                "source": "yfinance",
            }
        except Exception:
            continue

    return out

class YFinancePriceProvider:
    """
    Provider used by poll_prices.py.

    Contract:
      fetch_last_prices(ticker_map) -> { "SPY": {"ts_ms":..., "price":...}, ... }
    """

    def fetch_last_prices(self, ticker_map: Dict[str, str]) -> Dict[str, dict]:
        return fetch_last_prices_yf(ticker_map)


def fetch_latest_ohlcv_yf(ticker_map: Dict[str, str], interval: str = "1m") -> Dict[str, dict]:
    """
    Fetch latest OHLCV bar for each ticker via yfinance history.
    Returns:
      { "SPY": {"ts_ms":..., "o":..,"h":..,"l":..,"c":..,"v":.., "tf_s":60}, ... }
    """
    out: Dict[str, dict] = {}
    tf_s = 60 if interval == "1m" else 300 if interval == "5m" else 60
    now_ms = int(time.time() * 1000)

    for sym, tkr in (ticker_map or {}).items():
        try:
            t = yf.Ticker(tkr)
            hist = t.history(period="2d", interval=interval)
            if hist is None or hist.empty:
                continue

            last = hist.iloc[-1]
            o = float(last["Open"])
            h = float(last["High"])
            l = float(last["Low"])
            c = float(last["Close"])
            v = None
            try:
                vv = last.get("Volume", None)
                if vv is not None:
                    v = float(vv)
            except Exception:
                v = None

            out[str(sym)] = {
                "ts_ms": now_ms,
                "tf_s": int(tf_s),
                "o": float(o),
                "h": float(h),
                "l": float(l),
                "c": float(c),
                "v": (float(v) if v is not None else None),
            }
        except Exception:
            continue
