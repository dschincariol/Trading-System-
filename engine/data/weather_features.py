# CREATE NEW FILE: dev_core/weather_features.py
# (copy/paste entire file)

# dev_core/weather_features.py
import os
import json
import math
from typing import Dict, Any, List, Optional, Tuple

from engine.runtime.storage import connect


# ------------------------------------------------------------
# Config
# ------------------------------------------------------------

WEATHER_PROVIDER = os.environ.get("WEATHER_PROVIDER", "open_meteo").strip().lower()
WEATHER_REGION_MAP = os.environ.get("WEATHER_REGION_MAP", os.path.join("data", "weather_region_map.json"))

# Degree-day base temperature (F). Convert to C when needed.
DD_BASE_F = float(os.environ.get("WEATHER_DD_BASE_F", "65.0"))

# Forecast horizons (days) used to build compact features
HORIZON_3D = int(os.environ.get("WEATHER_HORIZON_3D", "3"))
HORIZON_7D = int(os.environ.get("WEATHER_HORIZON_7D", "7"))


# ------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------

def _day_start_utc_ms(ts_ms: int) -> int:
    # 00:00 UTC day boundary
    return (int(ts_ms) // 86_400_000) * 86_400_000


def _load_region_map() -> Dict[str, Any]:
    global _REGION_MAP_CACHE
    if _REGION_MAP_CACHE is not None:
        return _REGION_MAP_CACHE
    try:
        with open(WEATHER_REGION_MAP, "r", encoding="utf-8") as f:
            _REGION_MAP_CACHE = json.load(f) or {}
            return _REGION_MAP_CACHE
    except Exception:
        return {}


def _symbol_regions(symbol: str, cfg: Dict[str, Any]) -> List[Tuple[str, float]]:
    """
    Returns list of (region_id, weight). Weight defaults to 1.0.
    Config format supports:
      {
        "symbols": {
          "SPY": ["us_pop"],
          "OIL": [{"region_id":"us_gulf","weight":1.0}]
        }
      }
    """
    sym_u = str(symbol).upper()
    syms = (cfg or {}).get("symbols") or {}
    v = syms.get(sym_u)

    out: List[Tuple[str, float]] = []
    if not v:
        return out

    if isinstance(v, list):
        for it in v:
            if isinstance(it, str):
                out.append((it, 1.0))
            elif isinstance(it, dict):
                rid = it.get("region_id")
                w = it.get("weight", 1.0)
                if rid:
                    try:
                        out.append((str(rid), float(w)))
                    except Exception:
                        out.append((str(rid), 1.0))
    elif isinstance(v, str):
        out.append((v, 1.0))

    # Normalize weights
    s = sum(abs(w) for _, w in out) if out else 0.0
    if s > 1e-12:
        out = [(rid, float(w) / s) for rid, w in out]
    return out


def _latest_run_asof(con, provider: str, region_id: str, asof_ms: int) -> Optional[int]:
    row = con.execute(
        """
        SELECT MAX(run_ts)
        FROM weather_forecast_region_daily
        WHERE provider=? AND region_id=? AND run_ts <= ?
        """,
        (str(provider), str(region_id), int(asof_ms)),
    ).fetchone()
    if not row:
        return None
    try:
        v = int(row[0] or 0)
        return v if v > 0 else None
    except Exception:
        return None


def _fetch_days(con, provider: str, region_id: str, run_ts: int, day0_ms: int, dayN_ms: int) -> List[Dict[str, float]]:
    rows = con.execute(
        """
        SELECT day_ts, temp_mean_c, hdd65, cdd65, wind_mean_mps, precip_sum_mm, spread
        FROM weather_forecast_region_daily
        WHERE provider=? AND region_id=? AND run_ts=? AND day_ts BETWEEN ? AND ?
        ORDER BY day_ts ASC
        """,
        (str(provider), str(region_id), int(run_ts), int(day0_ms), int(dayN_ms)),
    ).fetchall()

    out = []
    for r in (rows or []):
        try:
            out.append({
                "day_ts": int(r[0]),
                "temp_mean_c": float(r[1] or 0.0),
                "hdd": float(r[2] or 0.0),
                "cdd": float(r[3] or 0.0),
                "wind_mean_mps": float(r[4] or 0.0),
                "precip_sum_mm": float(r[5] or 0.0),
                "spread": float(r[6] or 0.0),
            })
        except Exception:
            continue
    return out


def _active_alerts_score(con, provider: str, region_ids: List[str], ts_ms: int) -> float:
    """
    Simple severity-weighted score for active alerts.

    Note:
    - affected_regions is stored as JSON list of region_ids.
    - We count an alert as active if issued_ts <= ts_ms <= expires_ts (when expires exists).
    """
    if not region_ids:
        return 0.0

    # Pull recent alerts only (issued within 14 days) to keep query bounded.
    min_issued = int(ts_ms) - 14 * 24 * 3600 * 1000

    rows = con.execute(
        """
        SELECT issued_ts, expires_ts, severity, affected_regions
        FROM weather_alerts
        WHERE provider=? AND issued_ts >= ?
        ORDER BY issued_ts DESC
        """,
        (str(provider), int(min_issued)),
    ).fetchall()

    sev_w = {
        "extreme": 4.0,
        "severe": 3.0,
        "moderate": 2.0,
        "minor": 1.0,
    }

    score = 0.0
    for issued_ts, expires_ts, sev, aff in (rows or []):
        try:
            issued_ts = int(issued_ts or 0)
            expires_ts = int(expires_ts or 0) if expires_ts is not None else 0
            if issued_ts <= int(ts_ms) and (expires_ts == 0 or int(ts_ms) <= expires_ts):
                s = str(sev or "").strip().lower()
                w = float(sev_w.get(s, 0.5 if s else 0.0))
                aff_list = []
                try:
                    aff_list = json.loads(aff) if aff else []
                except Exception:
                    aff_list = []
                if not isinstance(aff_list, list):
                    aff_list = []
                if any(str(rid) in set(region_ids) for rid in aff_list):
                    score += w
        except Exception:
            continue

    # squash to ~[0,1] range
    return float(1.0 - math.exp(-0.25 * score)) if score > 0 else 0.0


# ------------------------------------------------------------
# Public API
# ------------------------------------------------------------

def get_weather_feature_snapshot(*, symbol: str, ts_ms: int) -> Dict[str, float]:
    """
    Leakage-safe weather snapshot for a decision time.

    Returns numeric keys:
      hdd_3d, hdd_7d, cdd_3d, cdd_7d,
      precip_7d, wind_3d, spread_7d, storm_risk

    If no mapping or no data is available, returns zeros.
    """
    cfg = _load_region_map()
    regions = _symbol_regions(symbol, cfg)
    if not regions:
        return {
            "hdd_3d": 0.0,
            "hdd_7d": 0.0,
            "cdd_3d": 0.0,
            "cdd_7d": 0.0,
            "precip_7d": 0.0,
            "wind_3d": 0.0,
            "spread_7d": 0.0,
            "storm_risk": 0.0,
        }

    day0 = _day_start_utc_ms(int(ts_ms))
    h3 = max(1, int(HORIZON_3D))
    h7 = max(1, int(HORIZON_7D))
    day3 = day0 + (h3 * 86_400_000) - 86_400_000
    day7 = day0 + (h7 * 86_400_000) - 86_400_000

    # Weighted aggregation across regions mapped to the symbol
    agg = {
        "hdd_3d": 0.0,
        "hdd_7d": 0.0,
        "cdd_3d": 0.0,
        "cdd_7d": 0.0,
        "precip_7d": 0.0,
        "wind_3d": 0.0,
        "spread_7d": 0.0,
    }

    con = connect()
    try:
        for region_id, w in regions:
            run_ts = _latest_run_asof(con, WEATHER_PROVIDER, str(region_id), int(ts_ms))
            if not run_ts:
                continue

            rows3 = _fetch_days(con, WEATHER_PROVIDER, str(region_id), int(run_ts), int(day0), int(day3))
            rows7 = _fetch_days(con, WEATHER_PROVIDER, str(region_id), int(run_ts), int(day0), int(day7))

            # sums / means
            def _sum(rows, k):
                return sum(float(r.get(k, 0.0) or 0.0) for r in (rows or []))

            def _mean(rows, k):
                rows = rows or []
                if not rows:
                    return 0.0
                return _sum(rows, k) / float(len(rows))

            agg["hdd_3d"] += float(w) * _sum(rows3, "hdd")
            agg["wind_3d"] += float(w) * _mean(rows3, "wind_mean_mps")

            agg["hdd_7d"] += float(w) * _sum(rows7, "hdd")
            agg["cdd_3d"] += float(w) * _sum(rows3, "cdd")
            agg["cdd_7d"] += float(w) * _sum(rows7, "cdd")
            agg["precip_7d"] += float(w) * _sum(rows7, "precip_sum_mm")
            agg["spread_7d"] += float(w) * _mean(rows7, "spread")

        region_ids = [rid for rid, _ in regions]
        storm_risk = _active_alerts_score(con, WEATHER_PROVIDER, region_ids, int(ts_ms))
 
    finally:
        try:
            con.close()
        except Exception:
            pass


    return {
        "hdd_3d": float(agg["hdd_3d"]),
        "hdd_7d": float(agg["hdd_7d"]),
        "cdd_3d": float(agg["cdd_3d"]),
        "cdd_7d": float(agg["cdd_7d"]),
        "precip_7d": float(agg["precip_7d"]),
        "wind_3d": float(agg["wind_3d"]),
        "spread_7d": float(agg["spread_7d"]),
        "storm_risk": float(storm_risk),
    }
