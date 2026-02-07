
# dev_core/social_risk.py
"""
Social manipulation / attention gating.

Read-only module:
- consumes social_features (as-of) and returns a suggested gate decision.
- NEVER emits orders, NEVER changes state outside annotations performed by caller.

This is intentionally conservative: prefer "do nothing" on missing data.
"""

from typing import Any, Dict

def social_gate_for_symbol(
    con,  # sqlite3 connection (already open in caller)
    symbol: str,
    ts_ms: int,
    *,
    bucket_sec: int = 300,
    manip_block_th: float = 0.85,
    shock_th: float = 0.80,
    shock_factor: float = 0.60,
) -> Dict[str, Any]:
    """
    Returns:
      {
        "block": bool,
        "factor": float (<=1),
        "manip_risk": float,
        "attention_shock": float,
        "promo_likelihood_mean": float,
      }
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {}

    try:
        row = con.execute(
            """
            SELECT
              manip_risk,
              attention_shock,
              promo_likelihood_mean
            FROM social_features
            WHERE symbol = ?
              AND bucket_sec = ?
              AND bucket_ts_ms <= ?
            ORDER BY bucket_ts_ms DESC
            LIMIT 1
            """,
            (sym, int(bucket_sec), int(ts_ms)),
        ).fetchone()
    except Exception:
        row = None

    if not row:
        return {"block": False, "factor": 1.0}

    manip = float(row[0] or 0.0)
    shock = float(row[1] or 0.0)
    promo = float(row[2] or 0.0)

    # Prefer blocking only when manipulation risk is high AND
    # signal is NOT cross-platform confirmed
    if manip >= float(manip_block_th) and float(row[2] or 0.0) < 1.0:

        return {
            "block": True,
            "factor": 0.0,
            "manip_risk": float(manip),
            "attention_shock": float(shock),
            "promo_likelihood_mean": float(promo),
        }

    if shock >= float(shock_th):
        f = max(0.0, min(1.0, float(shock_factor)))
        return {
            "block": False,
            "factor": float(f),
            "manip_risk": float(manip),
            "attention_shock": float(shock),
            "promo_likelihood_mean": float(promo),
        }

    return {
        "block": False,
        "factor": 1.0,
        "manip_risk": float(manip),
        "attention_shock": float(shock),
        "promo_likelihood_mean": float(promo),
    }
