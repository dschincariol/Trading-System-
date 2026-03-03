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

from engine.asset_map import asset_class_for_symbol

# ------------            -- ------------------------------------------------------
# BASE FEATURE LAYOUT (KEEP ORDER STABLE)
# ------------            -- ------------------------------------------------------
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

# ------------            -- ------------------------------------------------------
# Optional feature flags (MUST MATCH train/predict)
# ------------            -- ------------------------------------------------------
USE_TECH_FEATURES = os.environ.get("USE_TECH_FEATURES", "0") == "1"
USE_STRESS_FEATURES = os.environ.get("USE_STRESS_FEATURES", "0") == "1"
USE_SOCIAL_FEATURES = os.environ.get("USE_SOCIAL_FEATURES", "0") == "1"
USE_SOCIAL_REGIME = os.environ.get("USE_SOCIAL_REGIME", "0") == "1"
USE_WEATHER_FEATURES = os.environ.get("USE_WEATHER_FEATURES", "0") == "1"
USE_FACTOR_UNIVERSE = os.environ.get("USE_FACTOR_UNIVERSE", "0") == "1"

def feature_set_tag() -> str:
    """
    Stable feature-set tag for model-key namespacing.

    IMPORTANT:
    - Any change here intentionally creates a NEW model namespace.
    """
    parts = ["base"]
    if USE_TECH_FEATURES:
        parts.append("tech")
    if USE_STRESS_FEATURES:
        parts.append("stress")
    if USE_WEATHER_FEATURES:
        parts.append("wx")
    if USE_SOCIAL_FEATURES:
        parts.append("social")
    if USE_SOCIAL_REGIME:
        parts.append("social_regime")
    if USE_FACTOR_UNIVERSE:
        parts.append("factors")
    return "+".join(parts)

# ------------            -- ------------------------------------------------------
# Source credibility priors
# ------------            -- ------------------------------------------------------
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

    # --------            -- ------------------------------------------------------
    # Optional: technical / market features (price-only, safe)
    # --------            -- ------------------------------------------------------
    if USE_TECH_FEATURES:
        try:
            from engine.tech_indicators import compute_tech_features
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

    # --------            -- ------------------------------------------------------
    # Optional: market stress features
    # --------            -- ------------------------------------------------------
    if USE_STRESS_FEATURES:
        try:
            from engine.market_stress import get_market_stress_snapshot
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

    # --------            -- ------------------------------------------------------
    # Optional: external factor universe (Tier-1 fixed-dim vector)
    # --------            -- ------------------------------------------------------
    if USE_FACTOR_UNIVERSE:
        try:
            from engine.factor_universe import FACTOR_FEATURE_DIM, get_factor_universe_vector
            fv = get_factor_universe_vector(ts_ms=ts_ms) or []
            if len(fv) != int(FACTOR_FEATURE_DIM):
                fv = [0.0] * int(FACTOR_FEATURE_DIM)
        except Exception:
            fv = [0.0] * int(FACTOR_FEATURE_DIM)  # strict dimension safety

        features.extend([float(x or 0.0) for x in fv])

    # --------            -- ------------------------------------------------------
    # Optional: social context features (attention/manipulation)
    # --------            -- ------------------------------------------------------
    if USE_SOCIAL_FEATURES:
        try:
            from engine.social_context import get_social_feature_vector
            sf = get_social_feature_vector(symbol=str(symbol), ts_ms=int(ts_ms)) or {}
        except Exception:
            sf = {}

        # Fixed social layout (keep stable)
        features.extend([
            float(sf.get("mention_rate_z", 0.0)),
            float(sf.get("unique_authors", 0.0)),
            float(sf.get("new_author_ratio", 0.0)),
            float(sf.get("sentiment_mean", 0.0)),
            float(sf.get("sentiment_dispersion", 0.0)),
            float(sf.get("manip_risk", 0.0)),
            float(sf.get("attention_shock", 0.0)),
            float(sf.get("promo_likelihood_mean", 0.0)),
        ])

    # --------            -- ------------------------------------------------------
    # Optional: weather forecast + weather alerts features
    # (exogenous, leakage-safe via as-of queries)
    # --------            -- ------------------------------------------------------
    if USE_WEATHER_FEATURES:
        try:
            from engine.weather_features import get_weather_feature_snapshot
            wx = get_weather_feature_snapshot(symbol=str(symbol), ts_ms=ts_ms) or {}
        except Exception:
            wx = {}

        features.extend([
            float(wx.get("hdd_3d", 0.0)),
            float(wx.get("hdd_7d", 0.0)),
            float(wx.get("cdd_3d", 0.0)),
            float(wx.get("cdd_7d", 0.0)),
            float(wx.get("precip_7d", 0.0)),
            float(wx.get("wind_3d", 0.0)),
            float(wx.get("spread_7d", 0.0)),
            float(wx.get("storm_risk", 0.0)),
        ])


    # --------            -- ------------------------------------------------------
    # Optional: social regime features (QUIET/CHURN/FEAR/MANIA)
    # --------            -- ------------------------------------------------------
    if USE_SOCIAL_REGIME:
        try:
            from engine.social_regime import get_social_regime_vector
            rg = get_social_regime_vector(symbol=str(symbol), ts_ms=int(ts_ms)) or {}
        except Exception:
            rg = {}

        # Stable layout (append-only)
        features.extend([
            float(rg.get("mania_score", 0.0)),
            float(rg.get("fear_score", 0.0)),
            float(rg.get("churn_score", 0.0)),
            float(rg.get("regime_quiet", 0.0)),
            float(rg.get("regime_churn", 0.0)),
            float(rg.get("regime_fear", 0.0)),
            float(rg.get("regime_mania", 0.0)),
            float(rg.get("regime_conf", 0.0)),
        ])

    return features
