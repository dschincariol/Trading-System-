# engine/risk/portfolio_risk_engine.py
"""
Institutional Portfolio Risk Engine (additive, non-breaking).

Implements:
- Portfolio exposure accounting: gross/net, per-symbol, per-asset-class
- Rolling vol + correlation/cov proxy from prices (engine.strategy.risk)
- Correlated exposure clusters (graph components) capped by budget
- Vol-adjusted per-symbol sizing caps
- Asset-class risk budgets
- Portfolio-level gross/net caps
- Portfolio vol targeting (scale all weights) + hard-block threshold
- Max drawdown throttle + hard-block (engine.strategy.drawdown_state)

Integration:
- Called from engine.strategy.portfolio BEFORE portfolio_risk_gate
- Writes:
    risk_state:
      portfolio_risk_block (0/1)
      portfolio_risk_info  (json)
  and snapshots to portfolio_risk_snapshots (created in storage.init_db)
"""

import json
import os
from typing import Any, Dict, List, Optional, Tuple

from engine.data.asset_map import asset_class_for_symbol
from engine.strategy.drawdown_state import get_current_drawdown
from engine.strategy.risk import realized_vol_from_prices, corr_from_prices
from engine.runtime.risk_state import set_state


# -----------------------------
# Enable / controls (env)
# -----------------------------
USE = os.environ.get("PORTFOLIO_USE_RISK_ENGINE", "1") == "1"

# Universe bound for cov/corr computations (top by abs weight)
MAX_SYMBOLS = int(os.environ.get("PORTFOLIO_RISK_MAX_SYMBOLS", "18"))

# Portfolio caps
MAX_GROSS = float(os.environ.get("PORTFOLIO_RISK_MAX_GROSS", os.environ.get("PORTFOLIO_GROSS_CAP", "1.00")))
MAX_NET = float(os.environ.get("PORTFOLIO_RISK_MAX_NET", "0.60"))

# Drawdown throttle/hard block
DD_THROTTLE_START = float(os.environ.get("PORTFOLIO_RISK_DD_THROTTLE_START", "0.06"))
DD_THROTTLE_MIN_SCALE = float(os.environ.get("PORTFOLIO_RISK_DD_THROTTLE_MIN_SCALE", "0.35"))
DD_HARD_BLOCK = float(os.environ.get("PORTFOLIO_RISK_DD_HARD_BLOCK", "0.15"))

# Portfolio vol proxy + targeting
VOL_LOOKBACK = int(os.environ.get("PORTFOLIO_RISK_VOL_LOOKBACK", os.environ.get("PORTFOLIO_VOL_LOOKBACK", "240")))
VOL_TARGET = float(os.environ.get("PORTFOLIO_RISK_VOL_TARGET", os.environ.get("PORTFOLIO_TARGET_VOL", "0.020")))
PORTFOLIO_VOL_HARD_BLOCK = float(os.environ.get("PORTFOLIO_RISK_VOL_HARD_BLOCK", "0.0"))  # 0 disables
PORTFOLIO_VOL_FLOOR = float(os.environ.get("PORTFOLIO_RISK_VOL_FLOOR", "0.005"))
PORTFOLIO_VOL_CEIL = float(os.environ.get("PORTFOLIO_RISK_VOL_CEIL", "0.080"))

# Per-symbol vol caps (vol-adjusted sizing caps)
USE_VOL_CAPS = os.environ.get("PORTFOLIO_RISK_USE_VOL_CAPS", "1") == "1"
SYMBOL_CAP_MAX_W = float(os.environ.get("PORTFOLIO_RISK_SYMBOL_CAP_MAX_W", "0.35"))
SYMBOL_CAP_MIN_MULT = float(os.environ.get("PORTFOLIO_RISK_SYMBOL_CAP_MIN_MULT", "0.20"))

# Correlated exposure cluster caps (graph components)
USE_CORR_CLUSTERS = os.environ.get("PORTFOLIO_RISK_USE_CORR_CLUSTERS", "1") == "1"
CORR_LOOKBACK = int(os.environ.get("PORTFOLIO_RISK_CORR_LOOKBACK", "240"))
CLUSTER_CORR_TH = float(os.environ.get("PORTFOLIO_RISK_CLUSTER_CORR_TH", "0.85"))
CLUSTER_MAX_GROSS = float(os.environ.get("PORTFOLIO_RISK_CLUSTER_MAX_GROSS", "0.45"))
CLUSTER_MAX_COMPONENTS = int(os.environ.get("PORTFOLIO_RISK_CLUSTER_MAX_COMPONENTS", "12"))

# Asset-class budgets
USE_ASSET_CLASS_BUDGETS = os.environ.get("PORTFOLIO_RISK_USE_ASSET_CLASS_BUDGETS", "1") == "1"
_ASSET_CLASS_BUDGETS_JSON = os.environ.get("PORTFOLIO_RISK_ASSET_CLASS_BUDGETS_JSON", "").strip()

# Strategy-level budgets
USE_STRATEGY_BUDGETS = os.environ.get("PORTFOLIO_RISK_USE_STRATEGY_BUDGETS", "1") == "1"
STRATEGY_MAX_GROSS = float(os.environ.get("PORTFOLIO_RISK_MAX_STRATEGY_GROSS", "0.60"))
STRATEGY_MAX_NET = float(os.environ.get("PORTFOLIO_RISK_MAX_STRATEGY_NET", "0.40"))

_DEFAULT_ASSET_CLASS_BUDGETS = {
    "EQUITY": 1.00,
    "CRYPTO": 0.35,
    "COMMODITY": 0.50,
    "FX": 0.50,
    "RATES": 0.60,
    "UNKNOWN": 0.40,
}

ASSET_CLASS_BUDGETS: Dict[str, float] = dict(_DEFAULT_ASSET_CLASS_BUDGETS)
if _ASSET_CLASS_BUDGETS_JSON:
    try:
        d = json.loads(_ASSET_CLASS_BUDGETS_JSON)
        if isinstance(d, dict):
            for k, v in d.items():
                ASSET_CLASS_BUDGETS[str(k).upper()] = float(v)
    except Exception:
        ASSET_CLASS_BUDGETS = dict(_DEFAULT_ASSET_CLASS_BUDGETS)


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
    sgn = _side_sign((row or {}).get("side", "FLAT"))
    # tolerate both conventions:
    # - weight is magnitude with side providing sign
    # - weight is already signed
    if w < 0.0:
        return float(w)
    return float(abs(w)) * float(sgn)


def _abs_weight(row: Optional[Dict[str, Any]]) -> float:
    return abs(_signed_weight(row))


def _gross(rows: Dict[str, Dict[str, Any]]) -> float:
    return float(sum(_abs_weight(v) for v in (rows or {}).values()))


def _net(rows: Dict[str, Dict[str, Any]]) -> float:
    return float(sum(_signed_weight(v) for v in (rows or {}).values()))


def _annotate(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> None:
    for sym in list((desired or {}).keys()):
        try:
            desired[sym].setdefault("reason", {})
            if not isinstance(desired[sym]["reason"], dict):
                desired[sym]["reason"] = {"raw": desired[sym]["reason"]}
            desired[sym]["reason"]["portfolio_risk_engine"] = dict(info)
        except Exception:
            pass


def _top_symbols_by_abs(desired: Dict[str, Dict[str, Any]], n: int) -> List[str]:
    items: List[Tuple[str, float]] = []
    for sym, row in (desired or {}).items():
        try:
            aw = _abs_weight(row)
            if aw > 0.0:
                items.append((str(sym), float(aw)))
        except Exception:
            pass
    items.sort(key=lambda t: t[1], reverse=True)
    if n > 0:
        items = items[: int(n)]
    return [s for s, _w in items]


def _apply_portfolio_caps(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = dict(desired or {})

    g = _gross(out)
    n = _net(out)

    info["caps_pre_gross"] = float(g)
    info["caps_pre_net"] = float(n)
    info["cap_max_gross"] = float(MAX_GROSS)
    info["cap_max_net"] = float(MAX_NET)

    # Gross cap: scale all abs weights proportionally
    if float(MAX_GROSS) > 0.0 and g > float(MAX_GROSS) + 1e-12:
        scale = float(MAX_GROSS) / float(g) if g > 1e-12 else 0.0
        for sym in list(out.keys()):
            try:
                sw = _signed_weight(out[sym])
                sgn = 1.0 if sw >= 0.0 else -1.0
                out[sym]["weight"] = float(abs(sw) * scale) * float(sgn)
                out[sym].setdefault("reason", {})
                if isinstance(out[sym]["reason"], dict):
                    out[sym]["reason"]["portfolio_gross_cap"] = {"pre": float(g), "cap": float(MAX_GROSS), "scale": float(scale)}
            except Exception:
                pass
        info["caps_gross_scaled"] = True
        info["caps_gross_scale"] = float(scale)

    # Net cap: if |net| exceeds, scale signed weights down around 0
    g2 = _gross(out)
    n2 = _net(out)

    if float(MAX_NET) > 0.0 and abs(n2) > float(MAX_NET) + 1e-12 and g2 > 1e-12:
        # scale signed weights toward 0 preserving signs
        scaleN = float(MAX_NET) / float(abs(n2)) if abs(n2) > 1e-12 else 0.0
        for sym in list(out.keys()):
            try:
                sw = _signed_weight(out[sym])
                out[sym]["weight"] = float(sw) * float(scaleN)
                out[sym].setdefault("reason", {})
                if isinstance(out[sym]["reason"], dict):
                    out[sym]["reason"]["portfolio_net_cap"] = {"pre": float(n2), "cap": float(MAX_NET), "scale": float(scaleN)}
            except Exception:
                pass
        info["caps_net_scaled"] = True
        info["caps_net_scale"] = float(scaleN)

    info["caps_post_gross"] = float(_gross(out))
    info["caps_post_net"] = float(_net(out))
    return out


def _apply_drawdown_throttle(desired: Dict[str, Dict[str, Any]], dd: float, info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = dict(desired or {})

    if dd <= 0.0:
        return out

    info["dd"] = float(dd)
    info["dd_throttle_start"] = float(DD_THROTTLE_START)
    info["dd_throttle_min_scale"] = float(DD_THROTTLE_MIN_SCALE)
    info["dd_hard_block"] = float(DD_HARD_BLOCK)

    if float(DD_HARD_BLOCK) > 0.0 and dd >= float(DD_HARD_BLOCK):
        info["dd_hard_block_hit"] = True
        return out

    if float(DD_THROTTLE_START) > 0.0 and dd >= float(DD_THROTTLE_START):
        # linear ramp from 1.0 at start -> min_scale at hard_block (or at start+0.10 if hard_block disabled)
        end = float(DD_HARD_BLOCK) if float(DD_HARD_BLOCK) > float(DD_THROTTLE_START) else (float(DD_THROTTLE_START) + 0.10)
        t = (dd - float(DD_THROTTLE_START)) / max(1e-9, (end - float(DD_THROTTLE_START)))
        t = max(0.0, min(1.0, float(t)))
        scale = 1.0 - t * (1.0 - float(max(0.0, min(1.0, float(DD_THROTTLE_MIN_SCALE)))))
        g = _gross(out)
        if g > 1e-12:
            for sym in list(out.keys()):
                try:
                    sw = _signed_weight(out[sym])
                    sgn = 1.0 if sw >= 0.0 else -1.0
                    out[sym]["weight"] = float(abs(sw) * scale) * float(sgn)
                    out[sym].setdefault("reason", {})
                    if isinstance(out[sym]["reason"], dict):
                        out[sym]["reason"]["drawdown_throttle"] = {"dd": float(dd), "scale": float(scale)}
                except Exception:
                    pass
            info["dd_throttle_applied"] = True
            info["dd_throttle_scale"] = float(scale)

    return out


def _apply_asset_class_budgets(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_ASSET_CLASS_BUDGETS:
        return dict(desired or {})

    out = dict(desired or {})
    by_cls: Dict[str, float] = {}
    for sym, row in (out or {}).items():
        try:
            cls = str(asset_class_for_symbol(str(sym)) or "UNKNOWN").upper()
        except Exception:
            cls = "UNKNOWN"
        by_cls[cls] = float(by_cls.get(cls, 0.0) + _abs_weight(row))

    info["asset_class_gross_pre"] = dict(sorted(by_cls.items(), key=lambda kv: kv[0]))

    hit: Dict[str, Any] = {}
    for cls, gross in list(by_cls.items()):
        cap = float(ASSET_CLASS_BUDGETS.get(str(cls).upper(), ASSET_CLASS_BUDGETS.get("UNKNOWN", 0.40)))
        if cap > 0.0 and float(gross) > float(cap) + 1e-12:
            scale = float(cap) / float(gross) if gross > 1e-12 else 0.0
            for sym in list(out.keys()):
                try:
                    cls2 = str(asset_class_for_symbol(str(sym)) or "UNKNOWN").upper()
                    if cls2 == str(cls).upper():
                        sw = _signed_weight(out[sym])
                        sgn = 1.0 if sw >= 0.0 else -1.0
                        out[sym]["weight"] = float(abs(sw) * scale) * float(sgn)
                        out[sym].setdefault("reason", {})
                        if isinstance(out[sym]["reason"], dict):
                            out[sym]["reason"]["asset_class_budget"] = {
                                "asset_class": str(cls).upper(),
                                "gross_pre": float(gross),
                                "cap": float(cap),
                                "scale": float(scale),
                            }
                except Exception:
                    pass
            hit[str(cls).upper()] = {"gross_pre": float(gross), "cap": float(cap), "scale": float(scale)}

    if hit:
        info["asset_class_budgets_hit"] = hit

    # post
    by_cls2: Dict[str, float] = {}
    for sym, row in (out or {}).items():
        try:
            cls = str(asset_class_for_symbol(str(sym)) or "UNKNOWN").upper()
        except Exception:
            cls = "UNKNOWN"
        by_cls2[cls] = float(by_cls2.get(cls, 0.0) + _abs_weight(row))
    info["asset_class_gross_post"] = dict(sorted(by_cls2.items(), key=lambda kv: kv[0]))

    return out


def _apply_symbol_vol_caps(con, desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_VOL_CAPS:
        return dict(desired or {})

    out = dict(desired or {})
    vol_map: Dict[str, float] = {}

    for sym in list(out.keys()):
        try:
            v = realized_vol_from_prices(con, str(sym), lookback=int(VOL_LOOKBACK))
            if v is None:
                continue
            vv = float(v)
            vv = max(float(PORTFOLIO_VOL_FLOOR), min(float(PORTFOLIO_VOL_CEIL), vv))
            vol_map[str(sym)] = float(vv)
        except Exception:
            continue

    info["symbol_vol_n"] = int(len(vol_map))

    hit: Dict[str, Any] = {}
    for sym, row in list(out.items()):
        sw = _signed_weight(row)
        aw = abs(sw)
        if aw <= 0.0:
            continue
        v = vol_map.get(str(sym))
        if v is None or v <= 1e-12:
            continue

        mult = float(VOL_TARGET) / float(v)
        mult = max(float(SYMBOL_CAP_MIN_MULT), min(1.0, float(mult)))

        cap = min(float(SYMBOL_CAP_MAX_W), float(SYMBOL_CAP_MAX_W) * float(mult))
        if aw > cap + 1e-12:
            scale = float(cap) / float(aw) if aw > 1e-12 else 0.0
            sgn = 1.0 if sw >= 0.0 else -1.0
            out[sym]["weight"] = float(abs(sw) * scale) * float(sgn)
            out[sym].setdefault("reason", {})
            if isinstance(out[sym]["reason"], dict):
                out[sym]["reason"]["symbol_vol_cap"] = {
                    "vol": float(v),
                    "target": float(VOL_TARGET),
                    "mult": float(mult),
                    "cap": float(cap),
                    "pre": float(aw),
                    "scale": float(scale),
                }
            hit[str(sym)] = {"vol": float(v), "cap": float(cap), "pre": float(aw), "scale": float(scale)}

    if hit:
        info["symbol_vol_caps_hit"] = hit

    return out


def _corr_graph_components(con, syms: List[str]) -> List[List[str]]:
    if not syms or len(syms) < 2:
        return []

    # Build adjacency (undirected) for abs(corr) >= threshold
    adj: Dict[str, List[str]] = {s: [] for s in syms}
    for i in range(len(syms)):
        for j in range(i + 1, len(syms)):
            a, b = syms[i], syms[j]
            try:
                c = corr_from_prices(con, a, b, lookback=int(CORR_LOOKBACK))
                if c is None:
                    continue
                if abs(_safe_float(c, 0.0)) >= float(CLUSTER_CORR_TH):
                    adj[a].append(b)
                    adj[b].append(a)
            except Exception:
                continue

    # DFS components
    seen = set()
    comps: List[List[str]] = []
    for s in syms:
        if s in seen:
            continue
        stack = [s]
        comp = []
        seen.add(s)
        while stack:
            x = stack.pop()
            comp.append(x)
            for y in adj.get(x, []):
                if y not in seen:
                    seen.add(y)
                    stack.append(y)
        if len(comp) >= 2:
            comps.append(comp)

    # deterministic ordering: larger gross candidates first later
    return comps


def _apply_corr_cluster_caps(con, desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_CORR_CLUSTERS:
        return dict(desired or {})

    out = dict(desired or {})
    syms = _top_symbols_by_abs(out, int(MAX_SYMBOLS))
    comps = _corr_graph_components(con, syms)

    # bound number of components (worst-case compute)
    if CLUSTER_MAX_COMPONENTS > 0 and len(comps) > int(CLUSTER_MAX_COMPONENTS):
        comps = comps[: int(CLUSTER_MAX_COMPONENTS)]

    hit = []
    for comp in comps:
        gross = 0.0
        for s in comp:
            gross += _abs_weight(out.get(s))
        if gross <= float(CLUSTER_MAX_GROSS) + 1e-12:
            continue

        scale = float(CLUSTER_MAX_GROSS) / float(gross) if gross > 1e-12 else 0.0
        for s in comp:
            try:
                sw = _signed_weight(out.get(s))
                sgn = 1.0 if sw >= 0.0 else -1.0
                out[s]["weight"] = float(abs(sw) * scale) * float(sgn)
                out[s].setdefault("reason", {})
                if isinstance(out[s]["reason"], dict):
                    out[s]["reason"]["corr_cluster_cap"] = {
                        "cluster": list(comp),
                        "gross_pre": float(gross),
                        "cap": float(CLUSTER_MAX_GROSS),
                        "scale": float(scale),
                        "corr_th": float(CLUSTER_CORR_TH),
                    }
            except Exception:
                pass

        hit.append({"cluster": list(comp), "gross_pre": float(gross), "cap": float(CLUSTER_MAX_GROSS), "scale": float(scale)})

    if hit:
        info["corr_cluster_caps_hit"] = hit

    return out


def _portfolio_vol_proxy(con, desired: Dict[str, Dict[str, Any]]) -> Optional[float]:
    syms = _top_symbols_by_abs(desired, int(MAX_SYMBOLS))
    if len(syms) < 2:
        return None

    # signed weights (use raw weights as exposure fractions)
    w = []
    vols = []
    for s in syms:
        row = (desired or {}).get(s) or {}
        sw = _signed_weight(row)
        aw = abs(sw)
        if aw <= 0.0:
            continue
        try:
            v = realized_vol_from_prices(con, s, lookback=int(VOL_LOOKBACK))
            if v is None:
                return None
            vv = float(v)
            vv = max(float(PORTFOLIO_VOL_FLOOR), min(float(PORTFOLIO_VOL_CEIL), vv))
        except Exception:
            return None
        w.append(float(sw))
        vols.append(float(vv))

    if len(w) < 2:
        return None

    # normalize by gross to avoid pathological scaling
    gross = sum(abs(x) for x in w)
    if gross <= 1e-12:
        return None
    wn = [float(x / gross) for x in w]

    # covariance proxy via corr
    var = 0.0
    for i in range(len(wn)):
        var += float(wn[i] * wn[i]) * float(vols[i] * vols[i])

    # pairwise cov
    for i in range(len(wn)):
        for j in range(i + 1, len(wn)):
            try:
                c = corr_from_prices(con, syms[i], syms[j], lookback=int(CORR_LOOKBACK))
                if c is None:
                    continue
                cc = max(-1.0, min(1.0, _safe_float(c, 0.0)))
                cov = float(vols[i]) * float(vols[j]) * float(cc)
                var += 2.0 * float(wn[i]) * float(wn[j]) * float(cov)
            except Exception:
                continue

    if var < 0.0:
        var = 0.0
    return float(var ** 0.5)


def _apply_portfolio_vol_target(con, desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    out = dict(desired or {})
    pv = _portfolio_vol_proxy(con, out)
    if pv is None:
        return out

    info["portfolio_vol_proxy"] = float(pv)
    info["portfolio_vol_target"] = float(VOL_TARGET)
    info["portfolio_vol_hard_block"] = float(PORTFOLIO_VOL_HARD_BLOCK)

    # Hard block if configured
    if float(PORTFOLIO_VOL_HARD_BLOCK) > 0.0 and float(pv) >= float(PORTFOLIO_VOL_HARD_BLOCK):
        info["portfolio_vol_hard_block_hit"] = True
        return out

    # Scale entire portfolio to target (if pv > target)
    if float(VOL_TARGET) > 0.0 and float(pv) > float(VOL_TARGET) + 1e-12:
        scale = float(VOL_TARGET) / float(pv) if pv > 1e-12 else 0.0
        for sym in list(out.keys()):
            try:
                sw = _signed_weight(out[sym])
                out[sym]["weight"] = float(sw) * float(scale)
                out[sym].setdefault("reason", {})
                if isinstance(out[sym]["reason"], dict):
                    out[sym]["reason"]["portfolio_vol_target"] = {"pre_vol": float(pv), "target": float(VOL_TARGET), "scale": float(scale)}
            except Exception:
                pass
        info["portfolio_vol_scaled"] = True
        info["portfolio_vol_scale"] = float(scale)

    return out


def _persist_snapshot(con, now_ms: int, info: Dict[str, Any]) -> None:
    try:
        con.execute(
            """
            INSERT OR REPLACE INTO portfolio_risk_snapshots(
              ts_ms, gross, net, vol_proxy, drawdown, blocked, info_json
            )
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                int(now_ms),
                float(info.get("final_gross", 0.0) or 0.0),
                float(info.get("final_net", 0.0) or 0.0),
                (float(info.get("portfolio_vol_proxy")) if info.get("portfolio_vol_proxy") is not None else None),
                (float(info.get("drawdown")) if info.get("drawdown") is not None else None),
                (1 if bool(info.get("blocked", False)) else 0),
                json.dumps(info or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
    except Exception:
        pass


def apply_portfolio_risk_engine(
    con,
    desired: Dict[str, Dict[str, Any]],
    state: Dict[str, Dict[str, Any]],
    now_ms: int,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    if not USE:
        try:
            set_state("portfolio_risk_block", "0")
            set_state("portfolio_risk_info", json.dumps({"enabled": False}, separators=(",", ":"), sort_keys=True))
        except Exception:
            pass
        return desired, {"enabled": False}

    info: Dict[str, Any] = {"enabled": True}

    # Drawdown snapshot
    dd = 0.0
    try:
        dd = float(get_current_drawdown(con))
    except Exception:
        dd = 0.0
    info["drawdown"] = float(dd)

    # Starting exposures
    info["cur_gross"] = float(_gross(state or {}))
    info["cur_net"] = float(_net(state or {}))

    out = dict(desired or {})

    # Drawdown hard-block check (fail-closed)
    blocked = False
    block_reason: Optional[Dict[str, Any]] = None
    if float(DD_HARD_BLOCK) > 0.0 and float(dd) >= float(DD_HARD_BLOCK):
        blocked = True
        block_reason = {"type": "drawdown_hard_block", "dd": float(dd), "threshold": float(DD_HARD_BLOCK)}

    # Apply drawdown throttle (soft) before budgets/caps
    try:
        out = _apply_drawdown_throttle(out, float(dd), info)
    except Exception:
        pass

    # Asset-class budgets
    try:
        out = _apply_asset_class_budgets(out, info)
    except Exception:
        pass

    # Strategy-level budgets (institutional layer)
    try:
        out = _apply_strategy_budgets(out, info)
    except Exception:
        pass

    # Per-symbol vol caps
    try:
        out = _apply_symbol_vol_caps(con, out, info)
    except Exception:
        pass

    # Correlated cluster caps
    try:
        out = _apply_corr_cluster_caps(con, out, info)
    except Exception:
        pass

    # Portfolio-level vol targeting / hard-block
    try:
        out = _apply_portfolio_vol_target(con, out, info)
        if bool(info.get("portfolio_vol_hard_block_hit", False)):
            blocked = True
            block_reason = {"type": "portfolio_vol_hard_block", "vol": float(info.get("portfolio_vol_proxy", 0.0) or 0.0), "threshold": float(PORTFOLIO_VOL_HARD_BLOCK)}
    except Exception:
        pass

    # Portfolio gross/net caps (final)
    try:
        out = _apply_portfolio_caps(out, info)
    except Exception:
        pass

    info["final_gross"] = float(_gross(out))
    info["final_net"] = float(_net(out))

    # Final block status
    info["blocked"] = bool(blocked)
    if block_reason is not None:
        info["block_reason"] = dict(block_reason)

    # Write risk_state (for execution-time hard block)
    try:
        set_state("portfolio_risk_block", "1" if blocked else "0")
        set_state("portfolio_risk_info", json.dumps(info or {}, separators=(",", ":"), sort_keys=True))
    except Exception:
        pass

    # Persist snapshot (best-effort)
    try:
        _persist_snapshot(con, int(now_ms), info)
    except Exception:
        pass

    _annotate(out, info)
    return out, info

def _apply_strategy_budgets(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    if not USE_STRATEGY_BUDGETS:
        return dict(desired or {})

    out = dict(desired or {})

    # Group exposures by source_alert_id
    strat_gross: Dict[str, float] = {}
    strat_net: Dict[str, float] = {}

    for sym, row in (out or {}).items():
        sid = str((row or {}).get("source_alert_id") or "UNKNOWN")
        sw = _signed_weight(row)
        strat_gross[sid] = float(strat_gross.get(sid, 0.0) + abs(sw))
        strat_net[sid] = float(strat_net.get(sid, 0.0) + sw)

    info["strategy_gross_pre"] = dict(strat_gross)
    info["strategy_net_pre"] = dict(strat_net)

    hit: Dict[str, Any] = {}

    for sid in strat_gross.keys():
        g = float(strat_gross.get(sid, 0.0))
        n = float(strat_net.get(sid, 0.0))

        scale = 1.0

        # Gross cap
        if STRATEGY_MAX_GROSS > 0.0 and g > STRATEGY_MAX_GROSS:
            scale = min(scale, float(STRATEGY_MAX_GROSS) / float(g) if g > 1e-12 else 0.0)

        # Net cap
        if STRATEGY_MAX_NET > 0.0 and abs(n) > STRATEGY_MAX_NET:
            scale = min(scale, float(STRATEGY_MAX_NET) / float(abs(n)) if abs(n) > 1e-12 else 0.0)

        if scale < 1.0:
            for sym, row in (out or {}).items():
                if str((row or {}).get("source_alert_id") or "UNKNOWN") == sid:
                    sw = _signed_weight(row)
                    out[sym]["weight"] = float(sw) * float(scale)
                    out[sym].setdefault("reason", {})
                    if isinstance(out[sym]["reason"], dict):
                        out[sym]["reason"]["strategy_budget"] = {
                            "strategy": sid,
                            "gross_pre": g,
                            "net_pre": n,
                            "scale": float(scale),
                            "max_gross": float(STRATEGY_MAX_GROSS),
                            "max_net": float(STRATEGY_MAX_NET),
                        }

            hit[sid] = {
                "gross_pre": g,
                "net_pre": n,
                "scale": float(scale),
            }

    if hit:
        info["strategy_budgets_hit"] = hit

    # Post snapshot
    strat_gross_post: Dict[str, float] = {}
    strat_net_post: Dict[str, float] = {}

    for sym, row in (out or {}).items():
        sid = str((row or {}).get("source_alert_id") or "UNKNOWN")
        sw = _signed_weight(row)
        strat_gross_post[sid] = float(strat_gross_post.get(sid, 0.0) + abs(sw))
        strat_net_post[sid] = float(strat_net_post.get(sid, 0.0) + sw)

    info["strategy_gross_post"] = strat_gross_post
    info["strategy_net_post"] = strat_net_post

    return out