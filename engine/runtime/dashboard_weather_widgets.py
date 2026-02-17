# CREATE NEW FILE: dev_core/dashboard_weather_widgets.py
"""
Dashboard helper queries for weather + impact widgets.

This file is backend-agnostic: it only reads SQLite and returns JSON-ready dicts.
You can import it from whatever dashboard server you already have.

Functions:
- get_weather_snapshot_for_symbol(symbol, ts_ms)
- get_weather_effect_summary(ts_ms=None)
- get_weather_alert_summary(ts_ms=None)
"""

import time
import json
from typing import Dict, Any, Optional, List

from engine.dev_core.storage import connect
from engine.dev_core.weather_features import get_weather_feature_snapshot


def _utc_ms() -> int:
    return int(time.time() * 1000)


def get_weather_snapshot_for_symbol(symbol: str, ts_ms: Optional[int] = None) -> Dict[str, Any]:
    if ts_ms is None:
        ts_ms = _utc_ms()
    wx = get_weather_feature_snapshot(symbol=str(symbol), ts_ms=int(ts_ms)) or {}
    return {
        "ts_ms": int(ts_ms),
        "symbol": str(symbol).upper(),
        "wx": dict(wx),
    }


def get_weather_effect_summary(ts_ms: Optional[int] = None) -> Dict[str, Any]:
    if ts_ms is None:
        ts_ms = _utc_ms()

    con = connect()
    try:
        rows = con.execute(
            """
            SELECT horizon_s, ts_ms,
                   base_rmse, wx_rmse, rmse_delta,
                   base_spearman, wx_spearman, spearman_delta,
                   n_eval
            FROM model_weather_effect
            WHERE key_type='global' AND key='global'
              AND ts_ms <= ?
            ORDER BY ts_ms DESC
            LIMIT 50
            """,
            (int(ts_ms),),
        ).fetchall() or []

        # latest per horizon
        best = {}
        for r in rows:
            h = int(r[0])
            if h in best:
                continue
            best[h] = {
                "horizon_s": h,
                "ts_ms": int(r[1]),
                "base_rmse": float(r[2] or 0.0),
                "wx_rmse": float(r[3] or 0.0),
                "rmse_delta": float(r[4] or 0.0),
                "base_spearman": float(r[5] or 0.0),
                "wx_spearman": float(r[6] or 0.0),
                "spearman_delta": float(r[7] or 0.0),
                "n_eval": int(r[8] or 0),
            }

        out = [best[k] for k in sorted(best.keys())]
        return {"ts_ms": int(ts_ms), "series": out}
    finally:
        try:
            con.close()
        except Exception:
            pass


def get_weather_alert_summary(ts_ms: Optional[int] = None) -> Dict[str, Any]:
    if ts_ms is None:
        ts_ms = _utc_ms()

    con = connect()
    try:
        # active alerts (expires_ts==0 means unknown -> treat as active for 24h)
        min_issued = int(ts_ms) - 7 * 24 * 3600 * 1000
        rows = con.execute(
            """
            SELECT provider, alert_id, issued_ts, effective_ts, expires_ts,
                   event, severity, urgency, certainty,
                   area_desc, affected_regions, headline
            FROM weather_alerts
            WHERE issued_ts >= ?
            ORDER BY issued_ts DESC
            LIMIT 200
            """,
            (int(min_issued),),
        ).fetchall() or []

        out = []
        for r in rows:
            try:
                issued = int(r[2] or 0)
                expires = int(r[4] or 0)
                active = (issued <= int(ts_ms)) and (
                    (expires == 0 and int(ts_ms) <= issued + 24 * 3600 * 1000) or (expires > 0 and int(ts_ms) <= expires)
                )
                if not active:
                    continue

                out.append({
                    "provider": str(r[0] or ""),
                    "alert_id": str(r[1] or ""),
                    "issued_ts": issued,
                    "effective_ts": int(r[3] or 0),
                    "expires_ts": expires,
                    "event": str(r[5] or ""),
                    "severity": str(r[6] or ""),
                    "urgency": str(r[7] or ""),
                    "certainty": str(r[8] or ""),
                    "area_desc": str(r[9] or ""),
                    "affected_regions": json.loads(r[10]) if r[10] else [],
                    "headline": str(r[11] or ""),
                })
            except Exception:
                continue

        return {"ts_ms": int(ts_ms), "active": out}
    finally:
        try:
            con.close()
        except Exception:
            pass
