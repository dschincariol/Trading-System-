"""
A.3 Feature expansion for event embeddings.

Produces a numeric feature vector that is APPENDED to the
sentence embedding at train/predict time.

IMPORTANT:
- Stored embeddings are NEVER changed.
- Feature order is STRICT and VERSIONED.
- Train and predict MUST use identical flags.
"""

import math
import time
import os
from typing import Dict

from asset_map import asset_class_for_symbol

# ------------------------------------------------------------------
# BASE FEATURE LAYOUT (KEEP ORDER STABLE)
# ------------------------------------------------------------------
#
# [0] source_credibility
# [1] log_recency_hours
# [2] normalized_text_len
# [3] scheduled_flag
# [4] session_asia
# [5] session_eu
# [6] session_us
# [7] asset_class_match
#
BASE_FEATURE_DIM = 8

# ------------------------------------------------------------------
# Optional feature flags (MUST MATCH train/predict)
# ------------------------------------------------------------------
USE_TECH_FEATURES = os.environ.get("USE_TECH_FEATURES", "0") == "1"
USE_STRESS_FEATURES = os.environ.get("USE_STRESS_FEATURES", "0") == "1"

# ------------------------------------------------------------------
# Source credibility priors
# ------------------------------------------------------------------
_SOURCE_CRED = {
    "rss:reuters": 0.9,
    "rss:bloomberg": 0.9,
    "rss:ft": 0.9,
    "rss:wsj": 0.9,
    "rss:coindesk": 0.8,
    "rss:cointelegraph": 0.7,
}


def _source_credibility(source: str) -> float:
    s = str(source or "").lower()
    for k, v in _SOURCE_CRED.items():
        if k in s:
            return float(v)
    return 0.5


def _recency_hours(ts_ms: int) -> float:
    age_h = max(0.0, (time.time() * 1000 - ts_ms) / 3_600_000)
    return math.log1p(age_h) / 6.0  # ~0–1


def _norm_text_len(title: str, body: str) -> float:
    n = len((title or "") + " " + (body or ""))
    return min(1.0, n / 1000.0)


def _is_scheduled(title: str) -> float:
    t = (title or "").lower()
    keywords = ("cpi", "ppi", "fed", "ecb", "boj", "earnings", "gdp", "jobs")
    return 1.0 if any(k in t for k in keywords) else 0.0


def _session_flags(ts_ms: int):
    h = time.gmtime(ts_ms / 1000).tm_hour
    return (
        1.0 if 0 <= h < 7 else 0.0,     # Asia
        1.0 if 7 <= h < 13 else 0.0,    # EU
        1.0 if 13 <= h < 22 else 0.0,   # US
    )


def build_feature_vector(*, event: Dict, symbol: str) -> list:
    """
    Returns a STRICTLY ORDERED feature list.

    Base length: 8
    Optional features append AFTER base.
    """
    ts_ms = int(event.get("ts_ms", 0))
    title = event.get("title", "")
    body = event.get("body", "")
    source = event.get("source", "")

    asia, eu, us = _session_flags(ts_ms)

    asset_class = asset_class_for_symbol(symbol)
    asset_match = 1.0 if asset_class and asset_class != "UNKNOWN" else 0.0

    features = [
        _source_credibility(source),
        _recency_hours(ts_ms),
        _norm_text_len(title, body),
        _is_scheduled(title),
        asia,
        eu,
        us,
        asset_match,
    ]

    # --------------------------------------------------------------
    # Optional: technical / market features (price-only, safe)
    # --------------------------------------------------------------
    if USE_TECH_FEATURES:
        try:
            from dev_core.tech_indicators import compute_tech_features
            tf = compute_tech_features(str(symbol), ts_ms) or {}
        except Exception:
            tf = {}

        features.extend([
            float(tf.get("kama_level", 0.0)),
            float(tf.get("kama_slope", 0.0)),
            float(tf.get("price_kama_z", 0.0)),
            float(tf.get("atr_14", 0.0)),
            float(tf.get("atr_pct", 0.0)),
            float(tf.get("rv_20", 0.0)),
            float(tf.get("vol_of_vol", 0.0)),
        ])

    # --------------------------------------------------------------
    # Optional: market stress features
    # --------------------------------------------------------------
    if USE_STRESS_FEATURES:
        try:
            from dev_core.market_stress import get_market_stress_snapshot
            ms = get_market_stress_snapshot(ts_ms=ts_ms) or {}
        except Exception:
            ms = {}

        features.extend([
            float(ms.get("z_vix", 0.0)),
            float(ms.get("z_vvix", 0.0)),
            float(ms.get("z_move", 0.0)),
            float(ms.get("z_term", 0.0)),
            float(ms.get("z_credit", 0.0)),
            float(ms.get("stress_score", 0.0)),
        ])

    return features
