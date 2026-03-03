
# dev_core/social_regime.py
"""
Read-only social regime accessors + deterministic regime classifier.

Regimes:
- QUIET: low attention
- CHURN: high disagreement / instability
- FEAR: negative sentiment + attention
- MANIA: positive sentiment + attention + new participants

Uses table: social_regimes (bucketed).
"""

import os
import math
from typing import Any, Dict, Optional

from engine.storage import connect

DEFAULT_BUCKET_SEC = int(os.environ.get("SOCIAL_DEFAULT_BUCKET_SEC", "300"))  # 5m


def _bucket_start(ts_ms: int, bucket_sec: int) -> int:
    b = int(bucket_sec) * 1000
    if b <= 0:
        b = int(DEFAULT_BUCKET_SEC) * 1000
    return int(ts_ms // b) * b


def classify_regime_from_features(sf: Dict[str, Any]) -> Dict[str, Any]:
    # inputs (all optional)
    z = float(sf.get("mention_rate_z", 0.0) or 0.0)
    s = float(sf.get("sentiment_mean", 0.0) or 0.0)
    d = float(sf.get("sentiment_dispersion", 0.0) or 0.0)
    n = float(sf.get("new_author_ratio", 0.0) or 0.0)
    x = float(sf.get("cross_platform_confirm", 0.0) or 0.0)

    # squashes
    att = 1.0 / (1.0 + math.exp(-0.9 * z))          # 0..1
    pos = max(0.0, min(1.0, (s + 0.05) * 8.0))      # small bias to zero
    neg = max(0.0, min(1.0, (-s + 0.05) * 8.0))
    dis = max(0.0, min(1.0, d * 6.0))
    newp = max(0.0, min(1.0, n))

    mania = max(0.0, min(1.0, 0.45 * att + 0.25 * pos + 0.20 * newp + 0.10 * x))
    fear  = max(0.0, min(1.0, 0.50 * att + 0.35 * neg + 0.15 * dis))
    churn = max(0.0, min(1.0, 0.40 * att + 0.40 * dis + 0.20 * (1.0 - abs(s) * 6.0)))

    # quiet baseline
    quiet = max(0.0, min(1.0, 1.0 - att))

    # pick label
    scores = {
        "QUIET": quiet,
        "CHURN": churn,
        "FEAR": fear,
        "MANIA": mania,
    }
    regime = max(scores.items(), key=lambda kv: float(kv[1]))[0]
    conf = float(scores[regime])

    return {
        "regime": regime,
        "regime_conf": conf,
        "mania_score": float(mania),
        "fear_score": float(fear),
        "churn_score": float(churn),
    }


def get_social_regime_vector(*, symbol: str, ts_ms: int, bucket_sec: Optional[int] = None) -> Dict[str, Any]:
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {}

    bsec = int(bucket_sec or DEFAULT_BUCKET_SEC)
    if bsec <= 0:
        bsec = int(DEFAULT_BUCKET_SEC)

    bts = _bucket_start(int(ts_ms), int(bsec))

    con = connect()
    try:
        row = con.execute(
            """
            SELECT
              regime,
              regime_conf,
              mania_score,
              fear_score,
              churn_score
            FROM social_regimes
            WHERE symbol = ?
              AND bucket_sec = ?
              AND bucket_ts_ms <= ?
            ORDER BY bucket_ts_ms DESC
            LIMIT 1
            """,
            (sym, int(bsec), int(bts)),
        ).fetchone()

        if not row:
            return {}

        reg = str(row[0] or "QUIET").upper()
        conf = float(row[1] or 0.0)

        return {
            "regime": reg,
            "regime_conf": conf,
            "mania_score": float(row[2] or 0.0),
            "fear_score": float(row[3] or 0.0),
            "churn_score": float(row[4] or 0.0),
            "regime_quiet": 1.0 if reg == "QUIET" else 0.0,
            "regime_churn": 1.0 if reg == "CHURN" else 0.0,
            "regime_fear": 1.0 if reg == "FEAR" else 0.0,
            "regime_mania": 1.0 if reg == "MANIA" else 0.0,
        }
    except Exception:
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass
