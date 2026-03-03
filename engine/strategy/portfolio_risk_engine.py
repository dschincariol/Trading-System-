# engine/strategy/portfolio_risk_engine.py
"""dev_core/portfolio_risk_engine.py

Production Portfolio Risk Engine (non-breaking, additive).

Integrates into engine.strategy.portfolio:
- Tracks: gross/net, per-symbol, per-asset-class, per-source_alert_id
- Estimates portfolio realized vol using prices table (realized vols + pairwise corr)
- Enforces:
  - Max portfolio drawdown hard block (optional)
  - Max correlated pair exposure (optional)
  - Vol-adjusted per-symbol sizing caps (optional)
  - Per-asset-class gross caps (optional)

No schema changes required. Persists last snapshot into portfolio_meta and
writes a compact status into risk_state for execution-time blocks.
"""

import json
import os
from typing import Any, Dict, Optional, Tuple

from engine.data.asset_map import asset_class_for_symbol
from engine.strategy.drawdown_state import get_current_drawdown
from engine.strategy.risk import realized_vol_from_prices, corr_from_prices
from engine.runtime.risk_state import set_state

USE = os.environ.get("PORTFOLIO_USE_RISK_ENGINE", "1") == "1"

DD_HARD_BLOCK = float(os.environ.get("PORTFOLIO_DD_HARD_BLOCK", "0.0"))

USE_VOL_CAPS = os.environ.get("PORTFOLIO_USE_VOL_CAPS", "1") == "1"
VOL_LOOKBACK = int(os.environ.get("PORTFOLIO_RISK_VOL_LOOKBACK", "240"))
VOL_TARGET = float(os.environ.get("PORTFOLIO_RISK_VOL_TARGET", os.environ.get("PORTFOLIO_TARGET_VOL", "0.020")))
VOL_CAP_MAX_W = float(os.environ.get("PORTFOLIO_VOL_CAP_MAX_W", "0.35"))
VOL_CAP_MIN_MULT = float(os.environ.get("PORTFOLIO_VOL_CAP_MIN_MULT", "0.20"))

USE_CORR_PAIR_CAP = os.environ.get("PORTFOLIO_USE_CORR_PAIR_CAP", "1") == "1"
CORR_LOOKBACK = int(os.environ.get("PORTFOLIO_RISK_CORR_LOOKBACK", "240"))
CORR_PAIR_THRESH = float(os.environ.get("PORTFOLIO_CORR_PAIR_THRESH", "0.90"))
CORR_PAIR_MAX_EXPOSURE = float(os.environ.get("PORTFOLIO_CORR_PAIR_MAX_EXPOSURE", "0.35"))
CORR_PAIR_MAX_SYMBOLS = int(os.environ.get("PORTFOLIO_CORR_PAIR_MAX_SYMBOLS", "18"))
CORR_PAIR_MAX_PAIRS = int(os.environ.get("PORTFOLIO_CORR_PAIR_MAX_PAIRS", "36"))

USE_ASSET_CLASS_CAPS = os.environ.get("PORTFOLIO_USE_ASSET_CLASS_CAPS", "1") == "1"
_ASSET_CLASS_CAPS_RAW = os.environ.get("PORTFOLIO_ASSET_CLASS_CAPS_JSON", "").strip()
ASSET_CLASS_CAPS: Dict[str, float] = {}
if _ASSET_CLASS_CAPS_RAW:
    try:
        d = json.loads(_ASSET_CLASS_CAPS_RAW)
        if isinstance(d, dict):
            ASSET_CLASS_CAPS = {str(k).upper(): float(v) for k, v in d.items()}
    except Exception:
        ASSET_CLASS_CAPS = {}
if not ASSET_CLASS_CAPS:
    ASSET_CLASS_CAPS = {
        "EQUITY": 1.00,
        "CRYPTO": 0.35,
        "COMMODITY": 0.50,
        "FX": 0.50,
        "RATES": 0.60,
        "UNKNOWN": 0.40,
    }

PORTFOLIO_VOL_HARD_BLOCK = float(os.environ.get("PORTFOLIO_VOL_HARD_BLOCK", "0.0"))


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if v != v:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _side_sign(side: Any) -> float:
    s = str(side or "FLAT").upper()
    if s == "LONG":
        return 1.0
    if s == "SHORT":
        return -1.0
    return 0.0


def _signed_weight(row: Optional[Dict[str, Any]]) -> float:
    if not row:
        return 0.0
    w = _safe_float((row or {}).get("weight", 0.0), 0.0)
    side = str((row or {}).get("side", "FLAT")).upper()
    sgn = _side_sign(side)
    # tolerate both conventions:
    # - weight is magnitude (>=0) with side giving sign
    # - weight is already signed
    if w < 0 and sgn != 0:
        return float(w)
    return float(abs(w)) * float(sgn)


def _abs_weight(row: Optional[Dict[str, Any]]) -> float:
    return abs(_signed_weight(row))


def _gross_from(rows: Dict[str, Dict[str, Any]]) -> float:
    return float(sum(_abs_weight(v) for v in (rows or {}).values()))


def _net_from(rows: Dict[str, Dict[str, Any]]) -> float:
    return float(sum(_signed_weight(v) for v in (rows or {}).values()))


def _group_exposure(rows: Dict[str, Dict[str, Any]], key_fn) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, row in (rows or {}).items():
        k = key_fn(sym, row)
        if not k:
            k = "UNKNOWN"
        out[str(k)] = float(out.get(str(k), 0.0) + _abs_weight(row))
    return out


def _annotate(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> None:
    for sym in list((desired or {}).keys()):
        try:
            desired[sym].setdefault("reason", {})
            if not isinstance(desired[sym]["reason"], dict):
                desired[sym]["reason"] = {"raw": desired[sym]["reason"]}
            desired[sym]["reason"]["portfolio_risk_engine"] = dict(info)
        except Exception:
            pass


def _asset_class_caps(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_ASSET_CLASS_CAPS:
        return desired
    out = dict(desired or {})
    by_cls = _group_exposure(out, lambda sym, _row: asset_class_for_symbol(str(sym)))
    info["asset_class_gross"] = dict(sorted(by_cls.items(), key=lambda kv: kv[0]))
    caps_hit = {}
    for cls, gross in by_cls.items():
        cap = float(ASSET_CLASS_CAPS.get(str(cls).upper(), ASSET_CLASS_CAPS.get("UNKNOWN", 0.40)))
        if cap > 0 and float(gross) > float(cap) + 1e-12:
            scale = float(cap) / float(gross) if gross > 1e-12 else 0.0
            for sym in list(out.keys()):
                try:
                    c2 = asset_class_for_symbol(str(sym))
                    if str(c2).upper() == str(cls).upper():
                        sw = _signed_weight(out[sym])
                        sgn = 1.0 if sw >= 0 else -1.0
                        out[sym]["weight"] = float(abs(sw) * scale * sgn)
                except Exception:
                    pass
            caps_hit[str(cls).upper()] = {"gross": float(gross), "cap": float(cap), "scale": float(scale)}
    if caps_hit:
        info["asset_class_caps_hit"] = caps_hit
    return out


def _vol_caps(con, desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_VOL_CAPS:
        return desired
    out = dict(desired or {})
    vol_map: Dict[str, float] = {}
    for sym in list(out.keys()):
        try:
            v = realized_vol_from_prices(con, str(sym), lookback=int(VOL_LOOKBACK))
            if v is None:
                continue
            vol_map[str(sym)] = float(v)
        except Exception:
            continue
    if vol_map:
        info["symbol_vol_n"] = int(len(vol_map))
    caps_hit = {}
    for sym, row in list(out.items()):
        sw = _signed_weight(row)
        aw = abs(sw)
        if aw <= 0:
            continue
        v = vol_map.get(str(sym))
        if v is None or v <= 1e-12:
            continue
        mult = float(VOL_TARGET) / float(v)
        mult = max(float(VOL_CAP_MIN_MULT), min(1.0, float(mult)))
        cap = min(float(VOL_CAP_MAX_W), float(VOL_CAP_MAX_W) * float(mult))
        if aw > cap + 1e-12:
            scale = float(cap) / float(aw) if aw > 1e-12 else 0.0
            sgn = 1.0 if sw >= 0 else -1.0
            out[sym]["weight"] = float(abs(sw) * scale * sgn)
            out[sym].setdefault("reason", {})
            if isinstance(out[sym]["reason"], dict):
                out[sym]["reason"]["vol_cap"] = {
                    "vol": float(v),
                    "target": float(VOL_TARGET),
                    "mult": float(mult),
                    "cap": float(cap),
                    "pre": float(aw),
                    "scale": float(scale),
                }
            caps_hit[str(sym)] = {"vol": float(v), "cap": float(cap), "pre": float(aw), "scale": float(scale)}
    if caps_hit:
        info["vol_caps_hit_n"] = int(len(caps_hit))
    return out


def _corr_pair_caps(con, desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_CORR_PAIR_CAP:
        return desired
    out = dict(desired or {})
    items = []
    for sym, row in (out or {}).items():
        aw = _abs_weight(row)
        if aw > 0:
            items.append((str(sym), float(aw)))
    items.sort(key=lambda t: t[1], reverse=True)
    if CORR_PAIR_MAX_SYMBOLS > 0:
        items = items[: int(CORR_PAIR_MAX_SYMBOLS)]
    syms = [s for s, _w in items]

    pairs = []
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            if len(pairs) >= int(CORR_PAIR_MAX_PAIRS):
                break
            a, b = syms[i], syms[j]
            try:
                c = corr_from_prices(con, a, b, lookback=int(CORR_LOOKBACK))
                if c is None:
                    continue
                cc = abs(_safe_float(c, 0.0))
                if cc >= float(CORR_PAIR_THRESH):
                    wa = _abs_weight(out.get(a))
                    wb = _abs_weight(out.get(b))
                    pairs.append((cc, a, b, wa, wb))
            except Exception:
                continue

    if not pairs:
        return out

    pairs.sort(reverse=True, key=lambda t: t[0])
    hit = []
    for cc, a, b, wa, wb in pairs:
        s = float(wa) + float(wb)
        if s <= float(CORR_PAIR_MAX_EXPOSURE) + 1e-12:
            continue
        scale = float(CORR_PAIR_MAX_EXPOSURE) / float(s) if s > 1e-12 else 0.0
        for sym in (a, b):
            try:
                sw = _signed_weight(out.get(sym))
                sgn = 1.0 if sw >= 0 else -1.0
                out[sym]["weight"] = float(abs(sw) * scale * sgn)
                out[sym].setdefault("reason", {})
                if isinstance(out[sym]["reason"], dict):
                    out[sym]["reason"]["corr_pair_cap"] = {
                        "pair": [str(a), str(b)],
                        "abs_corr": float(cc),
                        "pair_cap": float(CORR_PAIR_MAX_EXPOSURE),
                        "pair_pre": float(s),
                        "scale": float(scale),
                    }
            except Exception:
                pass
        hit.append({"a": a, "b": b, "abs_corr": float(cc), "pair_pre": float(s), "scale": float(scale)})

    if hit:
        info["corr_pair_caps_hit"] = hit[:20]

    return out


def _portfolio_vol_proxy(con, rows: Dict[str, Dict[str, Any]]) -> Optional[float]:
    items = []
    for sym, row in (rows or {}).items():
        aw = _abs_weight(row)
        if aw > 0:
            items.append((str(sym), float(aw)))
    items.sort(key=lambda t: t[1], reverse=True)
    items = items[: max(2, min(20, len(items)))]
    if len(items) < 2:
        return None

    syms = [s for s, _w in items]
    w_signed = []
    vols = []
    for s in syms:
        v = realized_vol_from_prices(con, s, lookback=int(VOL_LOOKBACK))
        if v is None:
            return None
        vols.append(float(v))
        w_signed.append(float(_signed_weight((rows or {}).get(s))))

    gross = sum(abs(x) for x in w_signed)
    if gross <= 1e-12:
        return None
    w = [float(x / gross) for x in w_signed]

    var = 0.0
    for i in range(len(syms)):
        var += float(w[i] * w[i]) * float(vols[i] * vols[i])

    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            c = corr_from_prices(con, syms[i], syms[j], lookback=int(CORR_LOOKBACK))
            if c is None:
                continue
            cc = max(-1.0, min(1.0, _safe_float(c, 0.0)))
            cov = float(vols[i]) * float(vols[j]) * float(cc)
            var += 2.0 * float(w[i]) * float(w[j]) * float(cov)

    if var < 0.0:
        var = 0.0
    return float(var ** 0.5)


def apply_portfolio_risk_engine(
    con,
    desired: Dict[str, Dict[str, Any]],
    state: Dict[str, Dict[str, Any]],
    now_ms: int,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    if not USE:
        return desired, {"enabled": False}

    info: Dict[str, Any] = {"enabled": True}

    dd = 0.0
    try:
        dd = float(get_current_drawdown(con))
    except Exception:
        dd = 0.0
    info["drawdown"] = float(dd)

    info["cur_gross"] = float(_gross_from(state or {}))
    info["cur_net"] = float(_net_from(state or {}))
    info["tgt_gross_pre"] = float(_gross_from(desired or {}))
    info["tgt_net_pre"] = float(_net_from(desired or {}))

    try:
        info["tgt_asset_class_gross_pre"] = _group_exposure(desired or {}, lambda sym, _row: asset_class_for_symbol(str(sym)))
    except Exception:
        pass

    try:
        info["tgt_strategy_gross_pre"] = _group_exposure(
            desired or {},
            lambda _sym, row: f"alert:{(row or {}).get('source_alert_id', None)}",
        )
    except Exception:
        pass

    out = dict(desired or {})

    try:
        out = _asset_class_caps(out, info)
    except Exception:
        pass

    try:
        out = _vol_caps(con, out, info)
    except Exception:
        pass

    try:
        out = _corr_pair_caps(con, out, info)
    except Exception:
        pass

    pv = None
    try:
        pv = _portfolio_vol_proxy(con, out)
    except Exception:
        pv = None

    if pv is not None:
        info["portfolio_vol_proxy"] = float(pv)

    info["tgt_gross_post"] = float(_gross_from(out or {}))
    info["tgt_net_post"] = float(_net_from(out or {}))

    blocked = False
    block_reason = None

    if DD_HARD_BLOCK > 0.0 and float(dd) >= float(DD_HARD_BLOCK):
        blocked = True
        block_reason = {"type": "dd_hard_block", "dd": float(dd), "threshold": float(DD_HARD_BLOCK)}

    if (not blocked) and PORTFOLIO_VOL_HARD_BLOCK > 0.0 and pv is not None and float(pv) >= float(PORTFOLIO_VOL_HARD_BLOCK):
        blocked = True
        block_reason = {"type": "vol_hard_block", "vol": float(pv), "threshold": float(PORTFOLIO_VOL_HARD_BLOCK)}

    info["blocked"] = bool(blocked)
    if block_reason is not None:
        info["block_reason"] = dict(block_reason)

    try:
        set_state("portfolio_risk_block", "1" if blocked else "0")
        set_state("portfolio_risk_info", json.dumps(info, separators=(",", ":"), sort_keys=True))
        if blocked:
            set_state("execution_pause", "1")
    except Exception:
        pass

    _annotate(out, info)
    return out, info