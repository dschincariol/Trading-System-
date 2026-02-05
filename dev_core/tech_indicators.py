# dev_core/tech_indicators.py
import os
import time
import math
from typing import List, Dict, Optional, Tuple

import numpy as np

from dev_core.storage import connect

# --------------------------------------------
# Runtime controls
# --------------------------------------------

TECH_LOOKBACK = int(os.environ.get("TECH_LOOKBACK", "400"))          # price points
ATR_N = int(os.environ.get("TECH_ATR_N", "14"))
RV_N = int(os.environ.get("TECH_RV_N", "20"))
VOV_N = int(os.environ.get("TECH_VOV_N", "60"))

KAMA_ER_N = int(os.environ.get("TECH_KAMA_ER_N", "10"))
KAMA_FAST = int(os.environ.get("TECH_KAMA_FAST", "2"))
KAMA_SLOW = int(os.environ.get("TECH_KAMA_SLOW", "30"))

# cache (very small; avoids repeated DB reads inside tight loops)
_CACHE_TTL_S = float(os.environ.get("TECH_CACHE_TTL_S", "3.0"))
_cache = {
    # (symbol, ts_ms) -> (ts_s_cached, features_dict)
    "items": {},
    "ts_s": 0.0,
}


def _load_prices(symbol: str, ts_ms: int, lookback: int) -> List[Tuple[int, float]]:
    """
    Returns ascending list[(ts_ms, price)] up to ts_ms inclusive.
    """
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT ts_ms, price
            FROM prices
            WHERE symbol=? AND ts_ms <= ?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (str(symbol), int(ts_ms), int(lookback)),
        ).fetchall()
    finally:
        try:
            con.close()
        except Exception:
            pass

    out = []
    for t, p in (rows or []):
        if t is None or p is None:
            continue
        try:
            out.append((int(t), float(p)))
        except Exception:
            continue

    out.reverse()
    return out


def _log_returns(px: np.ndarray) -> np.ndarray:
    px = np.asarray(px, dtype=float)
    if px.size < 2:
        return np.asarray([], dtype=float)
    p0 = px[:-1]
    p1 = px[1:]
    good = (p0 > 0) & (p1 > 0)
    if not np.any(good):
        return np.asarray([], dtype=float)
    r = np.log(p1[good] / p0[good])
    return np.asarray(r, dtype=float)


def realized_vol(px: np.ndarray, n: int) -> float:
    r = _log_returns(px)
    if r.size < max(3, int(n)):
        return 0.0
    w = r[-int(n):]
    v = float(np.std(w, ddof=1))
    if not math.isfinite(v):
        return 0.0
    return max(0.0, v)


def vol_of_vol(px: np.ndarray, rv_n: int, vov_n: int) -> float:
    r = _log_returns(px)
    if r.size < max(10, int(rv_n) + int(vov_n)):
        return 0.0

    # rolling realized-vol series
    rvs = []
    for i in range(int(rv_n), r.size + 1):
        w = r[i - int(rv_n): i]
        v = float(np.std(w, ddof=1)) if w.size >= 3 else 0.0
        if math.isfinite(v):
            rvs.append(v)

    if len(rvs) < max(5, int(vov_n)):
        return 0.0

    w2 = np.asarray(rvs[-int(vov_n):], dtype=float)
    vv = float(np.std(w2, ddof=1)) if w2.size >= 3 else 0.0
    if not math.isfinite(vv):
        return 0.0
    return max(0.0, vv)


def kama(px: np.ndarray, er_n: int, fast: int, slow: int) -> float:
    """
    Kaufman Adaptive Moving Average (single pass; returns last value).
    """
    px = np.asarray(px, dtype=float)
    if px.size < max(20, int(er_n) + 2):
        return float(px[-1]) if px.size else 0.0

    er_n = int(er_n)
    fast = int(fast)
    slow = int(slow)

    fast_sc = 2.0 / (fast + 1.0)
    slow_sc = 2.0 / (slow + 1.0)

    # start from SMA seed
    k = float(np.mean(px[:er_n]))

    for i in range(er_n, px.size):
        change = abs(px[i] - px[i - er_n])
        volatility = float(np.sum(np.abs(np.diff(px[i - er_n:i + 1]))))
        er = float(change / volatility) if volatility > 1e-12 else 0.0
        sc = (er * (fast_sc - slow_sc) + slow_sc) ** 2
        k = k + sc * (px[i] - k)

    if not math.isfinite(k):
        return 0.0
    return float(k)


def atr_proxy(px: np.ndarray, n: int) -> float:
    """
    ATR proxy using abs log-return magnitude * price (since we only store last price).
    This is not full OHLC ATR but works as a volatility-scale proxy.
    """
    px = np.asarray(px, dtype=float)
    if px.size < max(4, int(n) + 1):
        return 0.0
    r = _log_returns(px)
    if r.size < int(n):
        return 0.0
    w = np.abs(r[-int(n):])
    a = float(np.mean(w)) if w.size else 0.0
    # convert to price-scale using last price
    out = a * float(px[-1])
    if not math.isfinite(out):
        return 0.0
    return max(0.0, out)


def _zscore(x: float, xs: np.ndarray) -> float:
    xs = np.asarray(xs, dtype=float)
    if xs.size < 10:
        return 0.0
    m = float(np.mean(xs))
    s = float(np.std(xs, ddof=1)) if xs.size >= 3 else 0.0
    if s <= 1e-12:
        return 0.0
    z = (float(x) - m) / s
    if not math.isfinite(z):
        return 0.0
    return float(z)


def compute_tech_features(symbol: str, ts_ms: int) -> Dict[str, float]:
    """
    Returns stable, leakage-safe features computed from prices up to ts_ms.
    """
    key = (str(symbol).upper(), int(ts_ms))
    now_s = time.monotonic()

    try:
        item = _cache["items"].get(key)
        if item is not None:
            ts_cached_s, feats = item
            if (now_s - float(ts_cached_s)) <= float(_CACHE_TTL_S):
                return dict(feats)
    except Exception:
        pass

    series = _load_prices(str(symbol).upper(), int(ts_ms), int(TECH_LOOKBACK))
    px = np.asarray([p for _, p in series], dtype=float)

    out: Dict[str, float] = {}

    # price-derived
    last = float(px[-1]) if px.size else 0.0

    k = kama(px, KAMA_ER_N, KAMA_FAST, KAMA_SLOW)
    out["kama_level"] = float(k)

    # slope proxy: kama(t) - kama(t-5) using truncated tail
    if px.size >= max(50, KAMA_ER_N + 10):
        k2 = kama(px[:-5], KAMA_ER_N, KAMA_FAST, KAMA_SLOW)
        out["kama_slope"] = float(k - k2)
    else:
        out["kama_slope"] = 0.0

    a = atr_proxy(px, ATR_N)
    out["atr_14"] = float(a)
    out["atr_pct"] = float((a / last) if last > 0 else 0.0)

    rv = realized_vol(px, RV_N)
    out["rv_20"] = float(rv)

    vv = vol_of_vol(px, RV_N, VOV_N)
    out["vol_of_vol"] = float(vv)

    # normalized distance to KAMA (z-like using ATR proxy)
    if a > 1e-12:
        out["price_kama_z"] = float((last - float(k)) / float(a))
    else:
        out["price_kama_z"] = 0.0

    # --------------------------------------------
    # Global stress proxy via VIX (optional)
    # --------------------------------------------
    # If you are polling ^VIX into prices as symbol="VIX", we can compute:
    # - stress_vix_level
    # - stress_vix_z_60
    # - stress_vix_change_1d (1-step change, since we don't have daily bars)
    try:
        vix_series = _load_prices("VIX", int(ts_ms), 200)
        vix_px = np.asarray([p for _, p in vix_series], dtype=float)
        if vix_px.size >= 5:
            vix_last = float(vix_px[-1])
            out["stress_vix_level"] = float(vix_last)
            out["stress_vix_z_60"] = float(_zscore(vix_last, vix_px[-60:]))
            out["stress_vix_change_1d"] = float(vix_last - float(vix_px[-2]))
        else:
            out["stress_vix_level"] = 0.0
            out["stress_vix_z_60"] = 0.0
            out["stress_vix_change_1d"] = 0.0
    except Exception:
        out["stress_vix_level"] = 0.0
        out["stress_vix_z_60"] = 0.0
        out["stress_vix_change_1d"] = 0.0

    # cache
    try:
        _cache["items"][key] = (float(now_s), dict(out))
    except Exception:
        pass

    return out
