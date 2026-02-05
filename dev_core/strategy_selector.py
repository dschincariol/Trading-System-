# dev_core/strategy_selector.py
"""
C1-3 Strategy selector.

- Manual selection via env PORTFOLIO_STRATEGY in {'baseline','conservative'}
- Auto selection via env PORTFOLIO_STRATEGY='auto' based on SQLite table strategy_metrics:
    PRIMARY KEY(strategy_name, window_days)
    metrics_json includes fields from C1-2:
      net_calmar, sharpe_simple, turnover_avg, ts_ms, etc.

Switch cooldown guard uses portfolio_meta table (owned by dev_core.portfolio):
  last_strategy_name
  last_strategy_switch_ts_ms
"""

import json
import os
import time
from typing import Optional, Tuple, Dict, Any

PORTFOLIO_STRATEGY = os.environ.get("PORTFOLIO_STRATEGY", "baseline").strip().lower()
PORTFOLIO_STRATEGY_SWITCH_COOLDOWN_S = int(os.environ.get("PORTFOLIO_STRATEGY_SWITCH_COOLDOWN_S", "3600"))

STRATEGY_WINDOW_DAYS = int(os.environ.get("STRATEGY_WINDOW_DAYS", "30"))
STRATEGY_METRICS_MAX_AGE_S = int(os.environ.get("STRATEGY_METRICS_MAX_AGE_S", "86400"))  # 24h

KNOWN_STRATEGIES = ("baseline", "conservative")

def _now_ms() -> int:
    return int(time.time() * 1000)

def _get_meta(con, key: str) -> Optional[str]:
    row = con.execute("SELECT value FROM portfolio_meta WHERE key=?", (str(key),)).fetchone()
    return str(row[0]) if row and row[0] is not None else None

def _set_meta(con, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO portfolio_meta(key, value) VALUES(?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (str(key), str(value)),
    )

def _load_strategy_metrics(con, window_days: int) -> Dict[str, Dict[str, Any]]:
    """
    Returns dict[strategy_name] = {"ts_ms":..., "metrics":{...}}
    Safe if table does not exist (returns empty).
    """
    try:
        rows = con.execute(
            """
            SELECT strategy_name, ts_ms, metrics_json
            FROM strategy_metrics
            WHERE window_days=?
            """,
            (int(window_days),),
        ).fetchall()
    except Exception:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for name, ts_ms, mj in rows or []:
        try:
            metrics = json.loads(mj) if mj else {}
            out[str(name).strip().lower()] = {"ts_ms": int(ts_ms), "metrics": metrics}
        except Exception:
            continue
    return out

def _pick_best_from_metrics(metrics_by_name: Dict[str, Dict[str, Any]], now_ms: int) -> str:
    """
    Deterministic selection:
      1) highest net_calmar
      2) highest sharpe_simple
      3) lowest turnover_avg
    Ignores stale rows.
    """
    best_name = "baseline"
    best_key: Optional[Tuple[float, float, float]] = None

    stale_cutoff = int(now_ms) - int(max(0, STRATEGY_METRICS_MAX_AGE_S)) * 1000

    for name in KNOWN_STRATEGIES:
        r = metrics_by_name.get(name)
        if not r:
            continue
        ts_ms = int(r.get("ts_ms") or 0)
        if ts_ms <= 0 or ts_ms < stale_cutoff:
            continue

        m = r.get("metrics") or {}
        net_calmar = float(m.get("net_calmar", 0.0) or 0.0)
        sharpe = float(m.get("sharpe_simple", 0.0) or 0.0)
        turnover = float(m.get("turnover_avg", 0.0) or 0.0)

        # maximize net_calmar, sharpe; minimize turnover
        key = (net_calmar, sharpe, -turnover)

        if best_key is None or key > best_key:
            best_key = key
            best_name = name

    return best_name

def choose_strategy_name(con, now_ms: int) -> str:
    """
    Returns chosen strategy name.
    Enforces switch cooldown using portfolio_meta.
    """
    requested = (PORTFOLIO_STRATEGY or "baseline").strip().lower()
    if requested not in ("baseline", "conservative", "auto"):
        requested = "baseline"

    if requested == "auto":
        metrics_by_name = _load_strategy_metrics(con, window_days=int(STRATEGY_WINDOW_DAYS))
        desired = _pick_best_from_metrics(metrics_by_name, now_ms=int(now_ms))
    else:
        desired = requested

    last_name = (_get_meta(con, "last_strategy_name") or "").strip().lower() or "baseline"
    last_switch = _get_meta(con, "last_strategy_switch_ts_ms")
    last_switch_ms = 0
    if last_switch:
        try:
            last_switch_ms = int(last_switch)
        except Exception:
            last_switch_ms = 0

    # switch cooldown guard
    if desired != last_name:
        cooldown_ms = int(max(0, PORTFOLIO_STRATEGY_SWITCH_COOLDOWN_S)) * 1000
        if cooldown_ms > 0 and (int(now_ms) - int(last_switch_ms)) < cooldown_ms:
            return last_name

        _set_meta(con, "last_strategy_name", desired)
        _set_meta(con, "last_strategy_switch_ts_ms", str(int(now_ms)))
        return desired

    # populate once
    if not last_switch_ms:
        _set_meta(con, "last_strategy_name", last_name)
        _set_meta(con, "last_strategy_switch_ts_ms", str(int(now_ms)))

    return last_name

def load_strategy_module(name: str):
    """
    Returns a module with:
      build_desired(alerts, now_ms) -> desired dict
    """
    n = (name or "baseline").strip().lower()
    if n == "conservative":
        from dev_core.strategies import conservative as mod
        return mod
    from dev_core.strategies import baseline as mod
    return mod
