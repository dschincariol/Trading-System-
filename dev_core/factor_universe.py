# NEW FILE: dev_core/factor_universe.py
# (create this file exactly)

# dev_core/factor_universe.py
import json
import math
import time
from typing import Dict, List, Optional

from engine.dev_core.storage import connect

# ----------------------------------------------------------------------
# Canonical Tier-1 feature order (FIXED DIMENSION)
# Train and predict MUST stay consistent.
# ----------------------------------------------------------------------
FACTOR_FEATURE_ORDER: List[str] = [
    # Rates / liquidity (macro)
    "macro.us_10y_yield_z",
    "macro.us_10y_yield_d5",
    "macro.us_5y_yield_z",
    "macro.us_5y_yield_d5",
    "macro.us_curve_10y_5y_z",

    # Vol / options structure (proxied)
    "vol.vix_z",
    "vol.vix_d5",
    "vol.rv20_z",

    # Credit stress (proxied)
    "credit.hyg_lqd_spread_z",
    "credit.hyg_lqd_spread_d5",

    # Flows / positioning (proxied)
    "flows.spy_agg_ratio_z",
    "flows.spy_agg_ratio_d5",

    # NEW: Direct execution alpha factors
    "options.skew_25d_z",
    "options.skew_25d_d5",
    "flows.index_constituent_imbalance_z",
    "flows.index_constituent_imbalance_d5",
    "earnings.proximity_decay",
]

FACTOR_FEATURE_DIM = len(FACTOR_FEATURE_ORDER)


def _safe_float(x) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else 0.0
    except Exception:
        return 0.0


def put_factor_feature(
    con,
    *,
    feature_id: str,
    asof_ts: int,
    effective_ts: int,
    value: float,
    meta: Optional[Dict] = None,
) -> None:
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO factor_features
              (feature_id, asof_ts, effective_ts, value, meta_json)
            VALUES (?,?,?,?,?)
            """,
            (
                str(feature_id),
                int(asof_ts),
                int(effective_ts),
                _safe_float(value),
                json.dumps(meta or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        if owns:
            con.commit()
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def _get_feature_asof(con, feature_id: str, ts_ms: int) -> float:
    row = con.execute(
        """
        SELECT value
        FROM factor_features
        WHERE feature_id=?
          AND asof_ts <= ?
          AND effective_ts <= ?
        ORDER BY asof_ts DESC, effective_ts DESC
        LIMIT 1
        """,
        (str(feature_id), int(ts_ms), int(ts_ms)),
    ).fetchone()
    if not row:
        return 0.0
    return _safe_float(row[0])


def get_factor_universe_vector(con=None, ts_ms: Optional[int] = None) -> List[float]:
    """
    Read-only: returns FIXED-DIM vector in FACTOR_FEATURE_ORDER.

    Uses as-of join semantics:
    - Only values with asof_ts <= ts_ms are eligible.
    - effective_ts must also be <= ts_ms.
    """
    owns = False
    if con is None:
        con = connect()
        owns = True
    try:
        if ts_ms is None:
            ts_ms = int(time.time() * 1000)

        out: List[float] = []
        for fid in FACTOR_FEATURE_ORDER:
            out.append(_get_feature_asof(con, fid, int(ts_ms)))

        # Hard safety: never allow dimension drift
        if len(out) != FACTOR_FEATURE_DIM:
            raise RuntimeError(
                f"Factor universe dimension mismatch: got={len(out)} expected={FACTOR_FEATURE_DIM}"
            )

        return out
    finally:
        if owns:
            try:
                con.close()
            except Exception:
                pass


def load_factor_universe_snapshot(con=None, ts_ms: Optional[int] = None) -> Dict[str, float]:
    """
    Convenience for explain_json / dashboard: dict feature_id -> value (as-of).
    """
    vec = get_factor_universe_vector(con=con, ts_ms=ts_ms)
    return {fid: _safe_float(v) for fid, v in zip(FACTOR_FEATURE_ORDER, vec)}
