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

Additional:
  CAPITAL_PRESERVE_SIZE_MULT=0.40  # CPM sizing compression
  USE_ALPHA_DECAY=1               # apply alpha_remaining multiplier
  MODEL_NAME=embed_regressor      # used for regime_compat (fallback)
"""

import os
import time
from typing import Optional, Tuple, Dict, Any

from engine.dev_core.storage import connect
from engine.dev_core.risk_state import get_state
from engine.dev_core.regime_compat import regime_compat_multiplier

MAX_POS = float(os.environ.get("MAX_POSITION_FRACTION", "0.20"))  # 20% notional
Z_REF = float(os.environ.get("POSITION_Z_REF", "2.0"))            # z=2 => full scale (before conf)

MIN_CONF = float(os.environ.get("POSITION_MIN_CONF", "0.55"))
MIN_ABS_Z = float(os.environ.get("POSITION_MIN_ABS_Z", "0.75"))

USE_SIZE_POLICY = os.environ.get("USE_SIZE_POLICY", "0") == "1"
SIZE_POLICY_CACHE_TTL_S = float(os.environ.get("SIZE_POLICY_CACHE_TTL_S", "30"))

# Capital Preservation Mode (CPM) sizing compression
CAPITAL_PRESERVE_SIZE_MULT = float(os.environ.get("CAPITAL_PRESERVE_SIZE_MULT", "0.40"))

# Alpha decay
USE_ALPHA_DECAY = os.environ.get("USE_ALPHA_DECAY", "1") == "1"

_size_policy_cache = {
    "ts": 0.0,
    "buckets": 0,
    "points": None,  # list of dicts with {conf_lo, conf_hi, factor, ...}
}


def _get_size_policy_factor(confidence: float) -> Optional[Tuple[float, dict]]:
    """
    Returns (factor, point_obj) for given confidence using latest size_policy_points.
    Returns None if table/policy is missing.

    NOTE: cached for SIZE_POLICY_CACHE_TTL_S seconds.
    """
    c = max(0.0, min(1.0, float(confidence)))
    now = time.time()

    # cache hit
    if (
        _size_policy_cache.get("points") is not None
        and (now - float(_size_policy_cache.get("ts") or 0.0)) < float(SIZE_POLICY_CACHE_TTL_S)
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
        # latest policy
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

        # policy points
        try:
            pts = con.execute(
                """
                SELECT conf_lo, conf_hi, factor, n, mean_net_ret, std_net_ret
                FROM size_policy_points
                WHERE policy_id=?
                ORDER BY bucket_idx ASC
                """,
                (int(policy_id),),
            ).fetchall()
        except Exception:
            pts = []

        points = []
        for conf_lo, conf_hi, factor, n, mean_net_ret, std_net_ret in pts or []:
            points.append(
                {
                    "conf_lo": float(conf_lo or 0.0),
                    "conf_hi": float(conf_hi or 0.0),
                    "factor": float(factor or 1.0),
                    "n": int(n or 0),
                    "mean_net_ret": float(mean_net_ret or 0.0),
                    "std_net_ret": float(std_net_ret or 0.0),
                }
            )

        _size_policy_cache.update({"ts": now, "buckets": int(buckets or 0), "points": points})

        for p in points:
            try:
                if c >= float(p.get("conf_lo")) and c < float(p.get("conf_hi")):
                    return float(p.get("factor") or 1.0), dict(p)
            except Exception:
                continue

        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def position_from_signal(
    expected_z: float,
    confidence: float,
    alpha_remaining: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Returns sizing decision dict.

    Restores intended behavior that was broken by unreachable code in your file:
      - baseline sizing
      - optional alpha decay
      - optional size_policy factor
      - CPM compression
      - regime_compat multiplier + suppression (fail-open if missing)
    """
    z = float(expected_z)
    c = float(confidence)

    az = abs(z)
    if c < float(MIN_CONF) or az < float(MIN_ABS_Z):
        return {
            "direction": "FLAT",
            "size": 0.0,
            "notional_frac": 0.0,
            "reason": f"below_threshold conf<{MIN_CONF} or |z|<{MIN_ABS_Z}",
        }

    direction = "LONG" if z > 0 else "SHORT"

    # baseline: scale by both confidence and z magnitude, clipped
    raw = (az / max(1e-9, float(Z_REF))) * c
    size = max(0.0, min(float(MAX_POS), float(raw) * float(MAX_POS)))

    # Alpha decay multiplier (if provided)
    if USE_ALPHA_DECAY and alpha_remaining is not None:
        try:
            decay_mult = max(0.0, min(1.0, float(alpha_remaining)))
            size = max(0.0, min(float(MAX_POS), float(size) * float(decay_mult)))
        except Exception:
            pass

    # Execution-aware factor (learned on net returns)
    size_policy_blob = None
    if USE_SIZE_POLICY:
        got = _get_size_policy_factor(c)
        if got is not None:
            factor, point = got
            factor = max(0.0, float(factor))
            size = max(0.0, min(float(MAX_POS), float(size) * float(factor)))
            size_policy_blob = {
                "factor": float(factor),
                "bucket": {
                    "conf_lo": point.get("conf_lo"),
                    "conf_hi": point.get("conf_hi"),
                    "n": point.get("n"),
                    "mean_net_ret": point.get("mean_net_ret"),
                    "std_net_ret": point.get("std_net_ret"),
                },
            }

    # Capital Preservation Mode: compress position size
    cap_mode = str(get_state("capital_mode", "normal") or "normal")
    cap_mult = 1.0
    if cap_mode == "preserve":
        try:
            cap_mult = max(0.0, min(1.0, float(CAPITAL_PRESERVE_SIZE_MULT)))
        except Exception:
            cap_mult = 1.0
        size = max(0.0, min(float(MAX_POS), float(size) * float(cap_mult)))

    # Regime compatibility (model × regime) sizing + suppression (fail-open)
    # Use model_registry if available; fallback to MODEL_NAME env.
    try:
        from engine.dev_core.model_registry import get_active_model_name  # type: ignore
        model_name = str(get_active_model_name() or "").strip() or ""
    except Exception:
        model_name = ""

    if not model_name:
        model_name = os.environ.get("MODEL_NAME", "embed_regressor").strip() or "embed_regressor"

    compat = {}
    try:
        # Your earlier code used anchor="SPY" in one path; keep that behavior.
        compat = regime_compat_multiplier(model_name=str(model_name), anchor="SPY") or {}
    except Exception:
        compat = {}

    if bool(compat.get("suppressed")):
        return {
            "direction": "FLAT",
            "size": 0.0,
            "notional_frac": 0.0,
            "reason": "regime_compat_suppressed",
            "regime_compat": compat,
            "alpha_remaining": (float(alpha_remaining) if alpha_remaining is not None else None),
        }

    try:
        mult = float(compat.get("mult") or 1.0)
    except Exception:
        mult = 1.0
    if mult < 0.0:
        mult = 0.0

    size = max(0.0, min(float(MAX_POS), float(size) * float(mult)))

    # output
    out: Dict[str, Any] = {
        "direction": direction,
        "size": float(size),
        "notional_frac": float(size),
        "reason": "scaled_by_confidence_z_regime_compat_and_decay",
        "regime_compat": compat,
        "alpha_remaining": (float(alpha_remaining) if alpha_remaining is not None else None),
    }

    if size_policy_blob is not None:
        out["size_policy"] = size_policy_blob

    if cap_mode == "preserve":
        out["capital_mode"] = "preserve"
        out["capital_preserve_mult"] = float(cap_mult)

    return out
