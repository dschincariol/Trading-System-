# NEW FILE: dev_core/regime_stack.py
# CREATE THIS FILE EXACTLY

"""
Hierarchical Regime Stack (3-layer weighted regime vector)

Layers (weights, not labels):
- Macro: risk_on/off, vol_expansion, credit_stress
- Asset-class: etf_like vs single_stock_like
- Microstructure: momentum_dominant, auction_heavy, news_shock

Also:
- regime_compatibility(profile, vector) -> [0,1]
- regime_model_version() string
"""

import math
import os
import json
import time
from typing import Any, Dict, Optional

from dev_core.storage import connect
from dev_core.factor_universe import _get_feature_asof as _get_factor_feature_asof


_REGIME_MODEL_VERSION = os.environ.get("REGIME_MODEL_VERSION", "regime_stack_v1")


def regime_model_version() -> str:
    return str(_REGIME_MODEL_VERSION)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clamp01(x: float) -> float:
    try:
        x = float(x)
    except Exception:
        return 0.0
    if x != x:
        return 0.0
    return float(max(0.0, min(1.0, x)))


def _sigmoid01(x: float, k: float = 1.0) -> float:
    try:
        x = float(x)
        k = float(k)
    except Exception:
        return 0.5
    if x != x:
        return 0.5
    try:
        return float(1.0 / (1.0 + math.exp(-k * x)))
    except Exception:
        return 0.5


def _z_to_weight(z: float, k: float = 1.0, center: float = 0.0) -> float:
    return _sigmoid01(float(z) - float(center), k=float(k))


def _safe_f(x: Any, d: float = 0.0) -> float:
    try:
        v = float(x)
        if v != v:
            return float(d)
        return float(v)
    except Exception:
        return float(d)


def _is_etf_like(sym: str) -> bool:
    s = str(sym or "").upper().strip()
    if not s:
        return False

    base = {
        "SPY",
        "QQQ",
        "IWM",
        "DIA",
        "VTI",
        "VOO",
        "IVV",
        "HYG",
        "LQD",
        "TLT",
        "IEF",
        "SHY",
        "GLD",
        "SLV",
        "USO",
        "UNG",
        "XLF",
        "XLK",
        "XLE",
        "XLV",
        "XLY",
        "XLP",
        "XLI",
        "XLB",
        "XLU",
        "XLC",
        "VIX",
    }

    raw = os.environ.get("REGIME_ETF_SYMBOLS", "").strip()
    if raw:
        try:
            extra = json.loads(raw)
            if isinstance(extra, list):
                for it in extra:
                    t = str(it or "").upper().strip()
                    if t:
                        base.add(t)
        except Exception:
            pass

    return s in base


def compute_regime_vector(
    *,
    symbol: Optional[str] = None,
    ts_ms: Optional[int] = None,
    con=None,
) -> Dict[str, Any]:
    sym = str(symbol or "").upper().strip()
    t = int(ts_ms or 0) or _now_ms()

    close_con = False
    if con is None:
        con = connect()
        close_con = True

    try:
        # ----------------------------
        # MACRO layer
        # ----------------------------
        try:
            vix_z = _safe_f(_get_factor_feature_asof(con, "vol.vix_z", int(t)), 0.0)
        except Exception:
            vix_z = 0.0

        try:
            rv20_z = _safe_f(_get_factor_feature_asof(con, "vol.rv20_z", int(t)), 0.0)
        except Exception:
            rv20_z = 0.0

        try:
            credit_z = _safe_f(_get_factor_feature_asof(con, "credit.hyg_lqd_spread_z", int(t)), 0.0)
        except Exception:
            credit_z = 0.0

        risk_off = _clamp01(_z_to_weight(vix_z, k=0.85, center=0.0))
        vol_expansion = _clamp01(_z_to_weight(rv20_z, k=0.85, center=0.0))
        credit_stress = _clamp01(_z_to_weight(credit_z, k=0.85, center=0.0))

        macro = {
            "risk_off": float(risk_off),
            "risk_on": float(_clamp01(1.0 - risk_off)),
            "vol_expansion": float(vol_expansion),
            "credit_stress": float(credit_stress),
        }

        # ----------------------------
        # ASSET layer
        # ----------------------------
        etf_like = 1.0 if _is_etf_like(sym) else 0.0
        asset = {
            "etf_like": float(etf_like),
            "single_stock_like": float(_clamp01(1.0 - etf_like)),
        }

        # ----------------------------
        # MICRO layer
        # ----------------------------
        try:
            from dev_core.tech_indicators import compute_tech_features
        except Exception:
            compute_tech_features = None

        kama_z = 0.0
        kama_slope = 0.0
        if compute_tech_features and sym:
            try:
                tf = compute_tech_features(sym, int(t)) or {}
                kama_z = _safe_f(tf.get("price_kama_z", 0.0), 0.0)
                kama_slope = _safe_f(tf.get("kama_slope", 0.0), 0.0)
            except Exception:
                kama_z = 0.0
                kama_slope = 0.0

        try:
            flow_z = _safe_f(_get_factor_feature_asof(con, "flows.spy_agg_ratio_z", int(t)), 0.0)
        except Exception:
            flow_z = 0.0

        mania = 0.0
        fear = 0.0
        churn = 0.0
        try:
            from dev_core.social_regime import get_social_regime_vector

            sv = get_social_regime_vector(symbol=sym, ts_ms=int(t)) or {}
            mania = _safe_f(sv.get("mania_score", 0.0), 0.0)
            fear = _safe_f(sv.get("fear_score", 0.0), 0.0)
            churn = _safe_f(sv.get("churn_score", 0.0), 0.0)
        except Exception:
            mania = 0.0
            fear = 0.0
            churn = 0.0

        momentum_dom = _clamp01(_z_to_weight(abs(kama_z) + abs(kama_slope) * 8.0, k=0.85, center=0.5))
        auction_heavy = _clamp01(_z_to_weight(abs(flow_z), k=0.85, center=0.75))
        news_shock = _clamp01(
            0.5 * _z_to_weight(mania, k=1.2, center=0.5) + 0.5 * _z_to_weight(fear, k=1.2, center=0.5)
        )

        micro = {
            "momentum_dominant": float(momentum_dom),
            "auction_heavy": float(auction_heavy),
            "news_shock": float(news_shock),
            "social_churn": float(_clamp01(churn)),
        }

        return {
            "ts_ms": int(t),
            "version": regime_model_version(),
            "macro": macro,
            "asset": asset,
            "micro": micro,
        }

    finally:
        if close_con:
            try:
                con.close()
            except Exception:
                pass


def _flatten_regime_vector(v: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not isinstance(v, dict):
        return out
    for layer in ("macro", "asset", "micro"):
        lv = v.get(layer)
        if isinstance(lv, dict):
            for k, val in lv.items():
                out[f"{layer}.{k}"] = float(_safe_f(val, 0.0))
    return out


def _flatten_regime_profile(p: Dict[str, Any]) -> Dict[str, float]:
    if not isinstance(p, dict):
        return {}
    if any(k in p for k in ("macro", "asset", "micro")):
        return _flatten_regime_vector(p)
    out: Dict[str, float] = {}
    for k, val in p.items():
        try:
            out[str(k)] = float(_safe_f(val, 0.0))
        except Exception:
            continue
    return out


def regime_compatibility(profile: Dict[str, Any], vector: Dict[str, Any]) -> float:
    pv = _flatten_regime_profile(profile or {})
    vv = _flatten_regime_vector(vector or {})

    if not pv or not vv:
        return 1.0

    dot = 0.0
    p2 = 0.0
    v2 = 0.0

    for k, pval in pv.items():
        if k not in vv:
            continue
        vval = float(vv.get(k, 0.0))
        pval = float(pval)
        if pval < 0.0:
            pval = 0.0
        if vval < 0.0:
            vval = 0.0
        dot += pval * vval
        p2 += pval * pval
        v2 += vval * vval

    if p2 <= 1e-12 or v2 <= 1e-12:
        return 1.0

    c = float(dot / (math.sqrt(p2) * math.sqrt(v2)))
    if c != c:
        return 1.0
    return float(_clamp01(c))
