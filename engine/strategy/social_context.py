# dev_core/social_context.py
"""
Read-only social context accessors.

Purpose:
- Provide "as-of" social features for a symbol at a timestamp.
- Never writes trades or affects execution directly.
- Safe: returns empty dict on any failure.

Uses table: social_features (bucketed).
"""

import os
from typing import Any, Dict, Optional

from engine.runtime.storage import connect

DEFAULT_BUCKET_SEC = int(os.environ.get("SOCIAL_DEFAULT_BUCKET_SEC", "300"))  # 5m


def _bucket_start(ts_ms: int, bucket_sec: int) -> int:
    b = int(bucket_sec) * 1000
    if b <= 0:
        b = int(DEFAULT_BUCKET_SEC) * 1000
    return int(ts_ms // b) * b


def get_social_feature_vector(*, symbol: str, ts_ms: int, bucket_sec: Optional[int] = None) -> Dict[str, Any]:
    """
    Returns the most recent social_features row at or before ts_ms.
    """
    sym = str(symbol or "").upper().strip()
    if not sym:
        return {}

    bsec = int(bucket_sec or DEFAULT_BUCKET_SEC)
    if bsec <= 0:
        bsec = int(DEFAULT_BUCKET_SEC)

    # use bucket start <= ts_ms
    bts = _bucket_start(int(ts_ms), int(bsec))

    con = connect()
    try:
        row = con.execute(
            """
            SELECT
              mention_count,
              unique_authors,
              new_author_ratio,
              engagement_now,
              sentiment_mean,
              sentiment_dispersion,
              mention_rate_z,
              bot_likelihood_mean,
              promo_likelihood_mean,
              manip_risk,
              attention_shock,
              cross_platform_confirm,
              cross_platform_confirm
            FROM social_features
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

        return {
            "mention_count": int(row[0] or 0),
            "unique_authors": int(row[1] or 0),
            "new_author_ratio": float(row[2] or 0.0),
            "engagement_now": float(row[3] or 0.0),
            "sentiment_mean": float(row[4] or 0.0),
            "sentiment_dispersion": float(row[5] or 0.0),
            "mention_rate_z": float(row[6] or 0.0),
            "bot_likelihood_mean": float(row[7] or 0.0),
            "promo_likelihood_mean": float(row[8] or 0.0),
            "manip_risk": float(row[9] or 0.0),
            "attention_shock": float(row[10] or 0.0),
            "cross_platform_confirm": float(row[11] or 0.0),
        }

    except Exception:
        return {}
    finally:
        try:
            con.close()
        except Exception:
            pass
