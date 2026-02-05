# dev_core/position_sizing.py
"""
Position sizing from (expected_z, confidence).

- expected_z is in "sigma units" (z-score style)
- confidence is [0..1]

This module supports two sizing modes:

1) Baseline (legacy): scale by confidence and |z|.
2) Execution-aware sizing (NEW, opt-in): multiply baseline size by the
   latest learned size_policy factor for the confidence bucket. The policy
   is trained on *net* returns (execution costs included), so it naturally
   reduces sizing when execution quality worsens.

Env:
  USE_SIZE_POLICY=1                # enable execution-aware sizing (default: 0)
  SIZE_POLICY_CACHE_TTL_S=30       # DB read cache TTL
  MAX_POSITION_FRACTION=0.20
  POSITION_Z_REF=2.0
  POSITION_MIN_CONF=0.55
  POSITION_MIN_ABS_Z=0.75
"""

import os
import time
from typing import Optional, Tuple

from dev_core.storage import connect

MAX_POS = float(os.environ.get("MAX_POSITION_FRACTION", "0.20"))  # 20% notional
Z_REF = float(os.environ.get("POSITION_Z_REF", "2.0"))            # z=2 => full scale (before conf)

MIN_CONF = float(os.environ.get("POSITION_MIN_CONF", "0.55"))
MIN_ABS_Z = float(os.environ.get("POSITION_MIN_ABS_Z", "0.75"))

USE_SIZE_POLICY = os.environ.get("USE_SIZE_POLICY", "0") == "1"
SIZE_POLICY_CACHE_TTL_S = float(os.environ.get("SIZE_POLICY_CACHE_TTL_S", "30"))

_size_policy_cache = {
    "ts": 0.0,
    "buckets": 0,
    "points": None,  # list of dicts with {conf_lo, conf_hi, factor}
}


def _get_size_policy_factor(confidence: float) -> Optional[Tuple[float, dict]]:
    """
    Returns (factor, point_obj) for given confidence using latest size_policy_points.
    Returns None if table/policy is missing.
    """
    c = max(0.0, min(1.0, float(confidence)))

    now = time.time()
    if (
        _size_policy_cache.get("points") is not None
        and (now - float(_size_policy_cache.get("ts") or 0.0)) < SIZE_POLICY_CACHE_TTL_S
    ):
        pts = _size_policy_cache["points"] or []
        for p in pts:
            try:
                if c >= float(p.get("conf_lo")) and c < float(p.get("conf_hi")):
                    return float(p.get("factor") or 1.0), dict(p)
            except Exception:
                continue
        return None

    con = connect()
    try:
        try:
            row = con.execute(
                """
                SELECT id, buckets
                FROM size_policy
                ORDER BY ts_ms DESC
                LIMIT 1
                """
            ).fetchone()
        except Exception:
            row = None

        if not row:
            _size_policy_cache.update({"ts": now, "buckets": 0, "points": []})
            return None

        policy_id, buckets = row[0], row[1]

        try:
            pts = con.execute(
                """
                SELECT conf_lo, conf_hi, factor, n, mean_net_ret, std_net_ret
                FROM size_policy_points
                WHERE policy_id=?
                ORDER BY bucket_idx ASC
                """
                ,
                (int(policy_id),),
            ).fetchall()
        except Exception:
            pts = []

        points = []
        for conf_lo, conf_hi, factor, n, mean_net_ret, std_net_ret in pts or []:
            points.append({
                "conf_lo": float(conf_lo or 0.0),
                "conf_hi": float(conf_hi or 0.0),
                "factor": float(factor or 1.0),
                "n": int(n or 0),
                "mean_net_ret": float(mean_net_ret or 0.0),
                "std_net_ret": float(std_net_ret or 0.0),
            })

        _size_policy_cache.update({"ts": now, "buckets": int(buckets or 0), "points": points})

        for p in points:
            try:
                if c >= float(p.get("conf_lo")) and c < float(p.get("conf_hi")):
                    return float(p.get("factor") or 1.0), dict(p)
            except Exception:
                continue

        return None
    finally:
        con.close()


def position_from_signal(expected_z: float, confidence: float):
    z = float(expected_z)
    c = float(confidence)

    az = abs(z)
    if c < MIN_CONF or az < MIN_ABS_Z:
        return {
            "direction": "FLAT",
            "size": 0.0,
            "notional_frac": 0.0,
            "reason": f"below_threshold conf<{MIN_CONF} or |z|<{MIN_ABS_Z}",
        }

    direction = "LONG" if z > 0 else "SHORT"

    # baseline scale by both confidence and z magnitude, clipped
    raw = (az / max(1e-9, Z_REF)) * c
    size = max(0.0, min(MAX_POS, raw * MAX_POS))

    # Execution-aware factor (learned on net returns)
    if USE_SIZE_POLICY:
        got = _get_size_policy_factor(c)
        if got is not None:
            factor, point = got
            factor = max(0.0, float(factor))
            size = max(0.0, min(MAX_POS, float(size) * factor))
            return {
                "direction": direction,
                "size": float(size),
                "notional_frac": float(size),
                "reason": "size_policy_factor",
                "size_policy": {
                    "factor": float(factor),
                    "bucket": {
                        "conf_lo": point.get("conf_lo"),
                        "conf_hi": point.get("conf_hi"),
                        "n": point.get("n"),
                        "mean_net_ret": point.get("mean_net_ret"),
                        "std_net_ret": point.get("std_net_ret"),
                    },
                },
            }

    return {
        "direction": direction,
        "size": float(size),
        "notional_frac": float(size),
        "reason": "scaled_by_confidence_and_z",
    }
