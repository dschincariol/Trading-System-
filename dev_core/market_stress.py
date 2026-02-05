# dev_core/market_stress.py
import math
import time
from typing import Dict, Optional, Tuple, List

import numpy as np

from dev_core.storage import connect


_LOOKBACK = 240   # samples
_ZWIN = 120       # samples for zscore window


def _load_prices(con, symbol: str, ts_ms: int, n: int) -> List[Tuple[int, float]]:
    rows = con.execute(
        """
        SELECT ts_ms, price
        FROM prices
        WHERE symbol=? AND ts_ms <= ?
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (str(symbol), int(ts_ms), int(n)),
    ).fetchall()
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


def _zscore_last(px: np.ndarray, win: int) -> float:
    px = np.asarray(px, dtype=float)
    if px.size < max(20, int(win)):
        return 0.0
    w = px[-int(win):]
    mu = float(np.mean(w))
    sd = float(np.std(w, ddof=1)) if w.size >= 3 else 0.0
    if sd <= 1e-12:
        return 0.0
    z = (float(w[-1]) - mu) / sd
    if not math.isfinite(z):
        return 0.0
    return float(z)


def _safe_last(px: np.ndarray) -> float:
    try:
        v = float(px[-1])
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def _ratio(a: float, b: float) -> float:
    a = float(a)
    b = float(b)
    if b <= 1e-12:
        return 0.0
    r = a / b
    if not math.isfinite(r):
        return 0.0
    return float(r)


def get_market_stress_snapshot(con=None, ts_ms: Optional[int] = None) -> Dict[str, float]:
    """
    Pure read-only snapshot computed from the local `prices` table.
    Returns a stable dict suitable for /api exposure and for explain_json annotations.

    Stress score is 0..1 (higher = more stress).
    """
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)

        # ---- load series
        def load(sym: str) -> np.ndarray:
            s = _load_prices(con, sym, int(ts_ms), _LOOKBACK)
            return np.asarray([p for _, p in s], dtype=float)

        vix = load("VIX")
        vix1d = load("VIX1D")
        vix9d = load("VIX9D")
        vix3m = load("VIX3M")
        vvix = load("VVIX")
        move = load("MOVE")

        hyg = load("HYG")
        lqd = load("LQD")
        tlt = load("TLT")
        shy = load("SHY")

        # ---- levels
        vix_last = _safe_last(vix)
        vvix_last = _safe_last(vvix)
        move_last = _safe_last(move)

        # ---- term structure ratios (shape)
        vix1d_last = _safe_last(vix1d)
        vix9d_last = _safe_last(vix9d)
        vix3m_last = _safe_last(vix3m)

        ts_1d = _ratio(vix1d_last, vix_last)
        ts_9d = _ratio(vix9d_last, vix_last)
        ts_3m = _ratio(vix3m_last, vix_last)

        # ---- credit proxy: HY vs IG
        hyg_last = _safe_last(hyg)
        lqd_last = _safe_last(lqd)
        credit_ratio = _ratio(lqd_last, hyg_last)  # rises when HY underperforms IG

        # ---- rates proxy: long vs short treasury (risk-off)
        tlt_last = _safe_last(tlt)
        shy_last = _safe_last(shy)
        rates_ratio = _ratio(tlt_last, shy_last)

        # ---- zscores (comparable scale)
        z_vix = _zscore_last(vix, _ZWIN)
        z_vvix = _zscore_last(vvix, _ZWIN)
        z_move = _zscore_last(move, _ZWIN)

        z_ts_1d = _zscore_last(np.asarray([_ratio(_safe_last(load("VIX1D")[:i+1]), _safe_last(load("VIX")[:i+1])) for i in range(min(_LOOKBACK, max(1, vix.size)))] ,dtype=float), min(_ZWIN, max(20, vix.size)))
        z_ts_9d = _zscore_last(np.asarray([_ratio(_safe_last(load("VIX9D")[:i+1]), _safe_last(load("VIX")[:i+1])) for i in range(min(_LOOKBACK, max(1, vix.size)))] ,dtype=float), min(_ZWIN, max(20, vix.size)))
        z_ts_3m = _zscore_last(np.asarray([_ratio(_safe_last(load("VIX3M")[:i+1]), _safe_last(load("VIX")[:i+1])) for i in range(min(_LOOKBACK, max(1, vix.size)))] ,dtype=float), min(_ZWIN, max(20, vix.size)))

        z_credit = _zscore_last(np.asarray([_ratio(_safe_last(lqd[:i+1]), _safe_last(hyg[:i+1])) for i in range(min(_LOOKBACK, max(1, hyg.size)))] ,dtype=float), min(_ZWIN, max(20, hyg.size)))
        z_rates = _zscore_last(np.asarray([_ratio(_safe_last(tlt[:i+1]), _safe_last(shy[:i+1])) for i in range(min(_LOOKBACK, max(1, shy.size)))] ,dtype=float), min(_ZWIN, max(20, shy.size)))

        # ---- unified stress score (0..1)
        # Weights chosen to emphasize volatility + cross-asset confirmation.
        w = {
            "vix": 0.30,
            "vvix": 0.20,
            "move": 0.20,
            "term": 0.15,
            "credit": 0.10,
            "rates": 0.05,
        }
        term_z = (z_ts_1d + z_ts_9d + z_ts_3m) / 3.0
        raw = (
            w["vix"] * z_vix
            + w["vvix"] * z_vvix
            + w["move"] * z_move
            + w["term"] * term_z
            + w["credit"] * z_credit
            + w["rates"] * z_rates
        )
        # squash into 0..1
        score = 1.0 / (1.0 + math.exp(-float(raw) / 2.0)) if math.isfinite(raw) else 0.5

        return {
            "ts_ms": float(ts_ms),

            "vix": float(vix_last),
            "vvix": float(vvix_last),
            "move": float(move_last),

            "vix1d_over_vix": float(ts_1d),
            "vix9d_over_vix": float(ts_9d),
            "vix3m_over_vix": float(ts_3m),

            "credit_lqd_over_hyg": float(credit_ratio),
            "rates_tlt_over_shy": float(rates_ratio),

            "z_vix": float(z_vix),
            "z_vvix": float(z_vvix),
            "z_move": float(z_move),
            "z_term": float(term_z),
            "z_credit": float(z_credit),
            "z_rates": float(z_rates),

            "stress_score": float(max(0.0, min(1.0, score))),
        }
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass
