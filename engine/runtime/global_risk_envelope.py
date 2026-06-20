# engine/runtime/global_risk_envelope.py
"""
Global Risk Envelope

Top-down deployable capital scaler applied AFTER hierarchical allocation.

Computes:
  - realized volatility proxy (portfolio-level)
  - portfolio drawdown proxy
  - execution degradation flag
  - optional broker health proxy (if table exists)

Returns:
  {
    "ok": bool,
    "ts_ms": int,
    "global_scale": float,   # 0..1
    "components": {...}
  }

Fail-open:
  If required data missing, returns scale=1.0 and ok=False.
"""

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple


VOL_TARGET = float(os.environ.get("GLOBAL_VOL_TARGET", "0.02"))  # target bucket vol
VOL_MAX_SCALE = float(os.environ.get("GLOBAL_VOL_MAX_SCALE", "1.0"))
VOL_MIN_SCALE = float(os.environ.get("GLOBAL_VOL_MIN_SCALE", "0.25"))

DD_GLOBAL_TH = float(os.environ.get("GLOBAL_DD_TH", "0.15"))
DD_GLOBAL_FLOOR = float(os.environ.get("GLOBAL_DD_FLOOR", "0.25"))

EXEC_DEGRADE_SCALE = float(os.environ.get("GLOBAL_EXEC_DEGRADE_SCALE", "0.5"))

WINDOW_S = int(os.environ.get("GLOBAL_ENVELOPE_WINDOW_S", "86400"))
BUCKET_S = int(os.environ.get("GLOBAL_ENVELOPE_BUCKET_S", "900"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if not math.isfinite(v):
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _table_exists(con, name: str) -> bool:
    try:
        r = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (str(name),),
        ).fetchone()
        return bool(r)
    except Exception:
        return False


def _bucket_ts(ts_ms: int) -> int:
    b = int(max(60, int(BUCKET_S)))
    return int((int(ts_ms) // int(b * 1000)) * int(b * 1000))


def _stddev(vals: List[float]) -> float:
    if not vals:
        return 0.0
    if len(vals) == 1:
        return 0.0
    m = sum(vals) / float(len(vals))
    var = sum((x - m) ** 2 for x in vals) / float(len(vals) - 1)
    return math.sqrt(max(0.0, var))


def _max_drawdown(pnl_series: List[Tuple[int, float]]) -> float:
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for _, pnl in pnl_series:
        eq += float(pnl)
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > max_dd:
            max_dd = dd
    denom = max(1e-9, abs(float(peak)))
    return float(max_dd) / float(denom)


def _read_portfolio_pnl(con, since_ms: int, now_ms: int) -> List[Tuple[int, float]]:
    # Prefer execution_capital_efficiency aggregated pnl
    if not _table_exists(con, "execution_capital_efficiency"):
        return []

    try:
        rows = con.execute(
            """
            SELECT ts_ms, pnl_net
            FROM execution_capital_efficiency
            WHERE ts_ms BETWEEN ? AND ?
            ORDER BY ts_ms ASC
            """,
            (int(since_ms), int(now_ms)),
        ).fetchall()
    except Exception:
        return []

    bucket_map: Dict[int, float] = {}
    for ts_ms, pnl in rows or []:
        try:
            bts = _bucket_ts(int(ts_ms or 0))
            bucket_map[bts] = float(bucket_map.get(bts, 0.0)) + _safe_float(pnl, 0.0)
        except Exception:
            continue

    return sorted([(int(k), float(v)) for k, v in bucket_map.items()], key=lambda x: x[0])


def _execution_degraded(con) -> bool:
    if not _table_exists(con, "system_readiness"):
        return False
    try:
        row = con.execute(
            """
            SELECT readiness_json
            FROM system_readiness
            ORDER BY ts_ms DESC
            LIMIT 1
            """
        ).fetchone()
        if not row or not row[0]:
            return False
        data = json.loads(row[0])
        return bool(data.get("execution_degraded"))
    except Exception:
        return False


def compute_global_risk_envelope(con, *, now_ms: Optional[int] = None) -> Dict[str, Any]:
    ts_ms = int(now_ms) if now_ms is not None else _now_ms()
    since_ms = int(ts_ms) - int(max(3600, WINDOW_S) * 1000)

    pnl_series = _read_portfolio_pnl(con, since_ms, ts_ms)
    if not pnl_series:
        return {
            "ok": False,
            "ts_ms": int(ts_ms),
            "global_scale": 1.0,
            "reason": "no_pnl_data",
        }

    bucket_returns = [float(p) for _, p in pnl_series]
    vol = _stddev(bucket_returns)
    dd = _max_drawdown(pnl_series)

    # Vol scaling
    if vol > 1e-9:
        vol_scale = float(VOL_TARGET) / float(vol)
    else:
        vol_scale = 1.0

    vol_scale = max(float(VOL_MIN_SCALE), min(float(VOL_MAX_SCALE), float(vol_scale)))

    # Drawdown scaling
    if float(dd) <= float(DD_GLOBAL_TH):
        dd_scale = 1.0
    else:
        sc = 1.0 - ((float(dd) - float(DD_GLOBAL_TH)) / float(DD_GLOBAL_TH))
        dd_scale = max(float(DD_GLOBAL_FLOOR), float(sc))

    # Execution degradation scaling
    exec_deg = _execution_degraded(con)
    exec_scale = float(EXEC_DEGRADE_SCALE) if exec_deg else 1.0

    global_scale = float(vol_scale) * float(dd_scale) * float(exec_scale)
    global_scale = max(0.0, min(1.0, float(global_scale)))

    return {
        "ok": True,
        "ts_ms": int(ts_ms),
        "global_scale": float(global_scale),
        "components": {
            "vol": float(vol),
            "vol_scale": float(vol_scale),
            "drawdown": float(dd),
            "dd_scale": float(dd_scale),
            "execution_degraded": bool(exec_deg),
            "exec_scale": float(exec_scale),
        },
    }