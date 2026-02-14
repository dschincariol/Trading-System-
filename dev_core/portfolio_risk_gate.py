# dev_core/portfolio_risk_gate.py
"""
Portfolio-level hard risk gate.

Purpose:
- Enforce net exposure limits, turnover limits, and drawdown-based add blocks
  *after* strategy outputs weights but *before* orders are emitted.

Env:
  PORTFOLIO_USE_RISK_GATE=1

  # Net exposure: net = sum(long_w) - sum(short_w)
  PORTFOLIO_MAX_NET_EXPOSURE=0.60

  # Turnover cap per rebalance: sum(abs(target_w - current_w)) across all symbols
  PORTFOLIO_MAX_TURNOVER=0.60

  # Drawdown-based add block: if dd >= threshold, block any increase in gross exposure
  PORTFOLIO_DD_ADD_BLOCK=0.08

  # Drawdown-based gross cap multiplier: if dd >= threshold, gross cap becomes
  # PORTFOLIO_GROSS_CAP * multiplier
  PORTFOLIO_DD_GROSS_MULT=0.70

Notes:
- This module does NOT emit orders. It only clamps desired targets.
- It annotates desired[sym]["reason"]["risk_gate"] for explainability.
"""

import os
from typing import Any, Dict, Tuple, List, Optional

from engine.dev_core.drawdown_state import get_current_drawdown
from engine.dev_core.weather_features import get_weather_feature_snapshot

USE = os.environ.get("PORTFOLIO_USE_RISK_GATE", "1") == "1"

MAX_NET = float(os.environ.get("PORTFOLIO_MAX_NET_EXPOSURE", "0.60"))
MAX_TURNOVER = float(os.environ.get("PORTFOLIO_MAX_TURNOVER", "0.60"))

DD_ADD_BLOCK = float(os.environ.get("PORTFOLIO_DD_ADD_BLOCK", "0.08"))
DD_GROSS_MULT = float(os.environ.get("PORTFOLIO_DD_GROSS_MULT", "0.70"))

GROSS_CAP = float(os.environ.get("PORTFOLIO_GROSS_CAP", "1.00"))

# ------            -- ------------------------------------------------------
# Optional: weather-aware portfolio clamps (read-only)
# ------            -- ------------------------------------------------------
USE_WX_RISK = os.environ.get("PORTFOLIO_USE_WEATHER_RISK", "1") == "1"

# If storm_risk >= threshold, block any increase in gross exposure
WX_STORM_ADD_BLOCK = float(os.environ.get("PORTFOLIO_WX_STORM_ADD_BLOCK", "0.60"))

# If storm_risk >= threshold, apply additional gross cap multiplier
WX_STORM_GROSS_MULT = float(os.environ.get("PORTFOLIO_WX_STORM_GROSS_MULT", "0.85"))

# Only evaluate top-N symbols by abs(target weight) to bound DB queries
WX_MAX_SYMBOLS = int(os.environ.get("PORTFOLIO_WX_MAX_SYMBOLS", "25"))


def _side_sign(side: str) -> float:
    s = str(side or "FLAT").upper()
    if s == "LONG":
        return 1.0
    if s == "SHORT":
        return -1.0
    return 0.0


def _cur_signed_weight(cur_row: Dict[str, Any]) -> float:
    if not cur_row:
        return 0.0
    w = float(cur_row.get("weight", 0.0) or 0.0)
    sgn = _side_sign(cur_row.get("side", "FLAT"))
    return float(w) * float(sgn)


def _tgt_signed_weight(tgt_row: Dict[str, Any]) -> float:
    if not tgt_row:
        return 0.0
    w = float(tgt_row.get("weight", 0.0) or 0.0)
    sgn = _side_sign(tgt_row.get("side", "FLAT"))
    return float(w) * float(sgn)


def _gross(desired: Dict[str, Dict[str, Any]]) -> float:
    g = 0.0
    for v in (desired or {}).values():
        try:
            g += abs(float(v.get("weight", 0.0) or 0.0))
        except Exception:
            pass
    return float(g)


def _net(desired: Dict[str, Dict[str, Any]]) -> float:
    n = 0.0
    for v in (desired or {}).values():
        try:
            n += _tgt_signed_weight(v)
        except Exception:
            pass
    return float(n)


def _turnover(desired: Dict[str, Dict[str, Any]], state: Dict[str, Dict[str, Any]]) -> float:
    syms = set()
    for s in (desired or {}).keys():
        syms.add(str(s))
    for s in (state or {}).keys():
        syms.add(str(s))

    tot = 0.0
    for sym in syms:
        cur = (state or {}).get(sym)
        tgt = (desired or {}).get(sym)
        cur_w = abs(_cur_signed_weight(cur))
        tgt_w = abs(_tgt_signed_weight(tgt))
        tot += abs(float(tgt_w) - float(cur_w))
    return float(tot)


def _portfolio_weather_risk(desired: Dict[str, Dict[str, Any]], now_ms: int) -> Dict[str, float]:
    """
    Portfolio-level weather summary computed from per-symbol weather snapshots.

    Returns:
      storm_risk_max: max storm risk across evaluated symbols
      storm_risk_w:   weight-weighted average storm risk (abs weights)
      spread_7d_w:    weight-weighted avg forecast spread
      n_eval:         number of symbols evaluated

    Bounded cost: only evaluates top WX_MAX_SYMBOLS by abs(target weight).
    """
    if not USE_WX_RISK:
        return {"storm_risk_max": 0.0, "storm_risk_w": 0.0, "spread_7d_w": 0.0, "n_eval": 0.0}

    # choose top-N by abs weight (stable + bounded)
    items = []
    for sym, row in (desired or {}).items():
        try:
            w = abs(float((row or {}).get("weight", 0.0) or 0.0))
            if w > 0.0:
                items.append((str(sym), float(w)))
        except Exception:
            pass
    items.sort(key=lambda t: t[1], reverse=True)
    if WX_MAX_SYMBOLS > 0:
        items = items[: int(WX_MAX_SYMBOLS)]

    denom = sum(w for _, w in items) if items else 0.0
    if denom <= 1e-12:
        return {"storm_risk_max": 0.0, "storm_risk_w": 0.0, "spread_7d_w": 0.0, "n_eval": 0.0}

    storm_max = 0.0
    storm_w = 0.0
    spread_w = 0.0
    n_eval = 0

    for sym, w in items:
        try:
            wx = get_weather_feature_snapshot(symbol=str(sym), ts_ms=int(now_ms)) or {}
            sr = float(wx.get("storm_risk", 0.0) or 0.0)
            sp = float(wx.get("spread_7d", 0.0) or 0.0)

            storm_max = max(storm_max, sr)
            storm_w += float(w) * sr
            spread_w += float(w) * sp
            n_eval += 1
        except Exception:
            continue

    return {
        "storm_risk_max": float(storm_max),
        "storm_risk_w": float(storm_w / denom) if denom > 1e-12 else 0.0,
        "spread_7d_w": float(spread_w / denom) if denom > 1e-12 else 0.0,
        "n_eval": float(n_eval),
    }


def _annotate(desired: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> None:
    for sym in list((desired or {}).keys()):
        try:
            desired[sym].setdefault("reason", {})
            if not isinstance(desired[sym]["reason"], dict):
                desired[sym]["reason"] = {"raw": desired[sym]["reason"]}
            desired[sym]["reason"]["risk_gate"] = dict(info)
        except Exception:
            pass


def apply_portfolio_risk_gate(
    con,
    desired: Dict[str, Dict[str, Any]],
    state: Dict[str, Dict[str, Any]],
    now_ms: int,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """
    Returns (desired_clamped, gate_info)
    """
    if not USE:
        return desired, {"enabled": False}

    out = dict(desired or {})
    info: Dict[str, Any] = {"enabled": True}

    # drawdown snapshot
    dd = 0.0
    try:
        dd = float(get_current_drawdown(con))
    except Exception:
        dd = 0.0

    info["drawdown"] = float(dd)

    # drawdown-based gross cap
    eff_gross_cap = float(GROSS_CAP)
    if dd >= float(DD_ADD_BLOCK):
        eff_gross_cap = float(GROSS_CAP) * float(DD_GROSS_MULT)

    # ------            -- ------------------------------------------------------
    # Optional: weather-based clamps (portfolio-level)
    # ------            -- ------------------------------------------------------
    wx = {"storm_risk_max": 0.0, "storm_risk_w": 0.0, "spread_7d_w": 0.0, "n_eval": 0.0}
    try:
        if USE_WX_RISK:
            wx = _portfolio_weather_risk(out, int(now_ms)) or wx
    except Exception:
        wx = wx

    info["wx_storm_risk_max"] = float(wx.get("storm_risk_max", 0.0) or 0.0)
    info["wx_storm_risk_w"] = float(wx.get("storm_risk_w", 0.0) or 0.0)
    info["wx_spread_7d_w"] = float(wx.get("spread_7d_w", 0.0) or 0.0)
    info["wx_n_eval"] = int(wx.get("n_eval", 0.0) or 0.0)

    # If storm risk is high, apply additional gross cap multiplier (fail-soft)
    if float(info["wx_storm_risk_max"]) >= float(WX_STORM_ADD_BLOCK):
        eff_gross_cap = min(float(eff_gross_cap), float(GROSS_CAP) * float(WX_STORM_GROSS_MULT))
        info["wx_gross_mult_applied"] = float(WX_STORM_GROSS_MULT)

    info["gross_cap"] = float(GROSS_CAP)
    info["eff_gross_cap"] = float(eff_gross_cap)

    # Enforce drawdown add-block: do not allow increasing gross exposure vs current state
    cur_gross = 0.0
    try:
        for _sym, cur in (state or {}).items():
            cur_gross += abs(_cur_signed_weight(cur))
    except Exception:
        cur_gross = 0.0
    info["cur_gross"] = float(cur_gross)

    tgt_gross = _gross(out)
    info["tgt_gross_pre"] = float(tgt_gross)

    wx_block = (
        (float(info.get("wx_storm_risk_max", 0.0)) >= float(WX_STORM_ADD_BLOCK)) if USE_WX_RISK else False
    )

    if (dd >= float(DD_ADD_BLOCK) or wx_block) and tgt_gross > cur_gross + 1e-12:
        # scale DOWN targets so gross <= current gross
        if tgt_gross > 1e-12:
            scale = float(cur_gross) / float(tgt_gross)
            for sym in list(out.keys()):
                try:
                    out[sym]["weight"] = float(out[sym].get("weight", 0.0) or 0.0) * float(scale)
                except Exception:
                    pass
            if dd >= float(DD_ADD_BLOCK):
                info["dd_add_block"] = True
                info["dd_add_scale"] = float(scale)
            if wx_block:
                info["wx_add_block"] = True
                info["wx_add_scale"] = float(scale)
        else:
            if dd >= float(DD_ADD_BLOCK):
                info["dd_add_block"] = True
                info["dd_add_scale"] = 0.0
            if wx_block:
                info["wx_add_block"] = True
                info["wx_add_scale"] = 0.0

    # Enforce effective gross cap (post dd scaling)
    tgt_gross2 = _gross(out)
    info["tgt_gross_post_dd"] = float(tgt_gross2)
    if tgt_gross2 > float(eff_gross_cap) and tgt_gross2 > 1e-12:
        scale = float(eff_gross_cap) / float(tgt_gross2)
        for sym in list(out.keys()):
            try:
                out[sym]["weight"] = float(out[sym].get("weight", 0.0) or 0.0) * float(scale)
            except Exception:
                pass
        info["gross_scale"] = float(scale)

    # Enforce max net exposure by scaling the overweight side only
    net = _net(out)
    info["net_pre"] = float(net)
    info["max_net"] = float(MAX_NET)

    if float(MAX_NET) > 0.0 and abs(net) > float(MAX_NET) + 1e-12:
        # If net too long -> scale LONG weights down
        # If net too short -> scale SHORT weights down
        if net > 0:
            side_to_scale = "LONG"
            denom = 0.0
            for _sym, tgt in out.items():
                if str(tgt.get("side", "FLAT")).upper() == "LONG":
                    denom += float(tgt.get("weight", 0.0) or 0.0)
            if denom > 1e-12:
                target_long_sum = denom - (abs(net) - float(MAX_NET))
                scale = max(0.0, float(target_long_sum) / float(denom))
                for _sym, tgt in out.items():
                    if str(tgt.get("side", "FLAT")).upper() == "LONG":
                        tgt["weight"] = float(tgt.get("weight", 0.0) or 0.0) * float(scale)
                info["net_scale_side"] = side_to_scale
                info["net_scale"] = float(scale)
        else:
            side_to_scale = "SHORT"
            denom = 0.0
            for _sym, tgt in out.items():
                if str(tgt.get("side", "FLAT")).upper() == "SHORT":
                    denom += float(tgt.get("weight", 0.0) or 0.0)
            if denom > 1e-12:
                target_short_sum = denom - (abs(net) - float(MAX_NET))
                scale = max(0.0, float(target_short_sum) / float(denom))
                for _sym, tgt in out.items():
                    if str(tgt.get("side", "FLAT")).upper() == "SHORT":
                        tgt["weight"] = float(tgt.get("weight", 0.0) or 0.0) * float(scale)
                info["net_scale_side"] = side_to_scale
                info["net_scale"] = float(scale)

    info["net_post"] = float(_net(out))

    # Enforce turnover cap by scaling *deltas* (keeps direction, reduces churn)
    to = _turnover(out, state or {})
    info["turnover_pre"] = float(to)
    info["max_turnover"] = float(MAX_TURNOVER)

    if float(MAX_TURNOVER) > 0.0 and to > float(MAX_TURNOVER) + 1e-12:
        # Scale targets toward current state: tgt = cur + k*(tgt-cur)
        k = float(MAX_TURNOVER) / float(to) if to > 1e-12 else 0.0
        syms = set()
        for s in (out or {}).keys():
            syms.add(str(s))
        for s in (state or {}).keys():
            syms.add(str(s))

        for sym in syms:
            cur = (state or {}).get(sym)
            tgt = (out or {}).get(sym)
            if not tgt:
                continue

            cur_abs = abs(_cur_signed_weight(cur))
            tgt_abs = abs(_tgt_signed_weight(tgt))
            new_abs = float(cur_abs) + float(k) * (float(tgt_abs) - float(cur_abs))
            if new_abs < 1e-12 or str(tgt.get("side", "FLAT")).upper() == "FLAT":
                tgt["side"] = "FLAT"
                tgt["weight"] = 0.0
            else:
                tgt["weight"] = float(max(0.0, new_abs))

        info["turnover_scale_k"] = float(k)

    info["turnover_post"] = float(_turnover(out, state or {}))

    _annotate(out, info)
    return out, info


def apply_execution_risk_governor(
    con,
    orders: List[Dict[str, Any]],
    *,
    broker: str,
    mode: str,
    equity_usd: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], dict]:
    """
    Execution-time risk governor (institutional layer):
    - global pause via risk_state key: execution_pause=1
    - caps per-symbol max abs weight (EXEC_MAX_ABS_WEIGHT)
    - caps per-symbol max abs delta weight (EXEC_MAX_ABS_DELTA_WEIGHT)
    - caps max orders per pass (EXEC_MAX_ORDERS_PER_PASS)
    """
    broker = str(broker or "").strip().lower()
    mode = str(mode or "").strip().lower()

    # global pause switch (fail closed)
    try:
        from engine.dev_core.risk_state import get_state

        if str(get_state("execution_pause", "0") or "0").strip() == "1":
            return [], {"ok": False, "status": "blocked_execution_pause", "broker": broker, "mode": mode}
    except Exception:
        return [], {"ok": False, "status": "blocked_risk_state_error", "broker": broker, "mode": mode}

    # caps (env)
    try:
        max_abs_w = float(os.environ.get("EXEC_MAX_ABS_WEIGHT", "0.35"))
        max_abs_dw = float(os.environ.get("EXEC_MAX_ABS_DELTA_WEIGHT", "0.15"))
        max_n = int(os.environ.get("EXEC_MAX_ORDERS_PER_PASS", "50"))
    except Exception:
        max_abs_w, max_abs_dw, max_n = 0.35, 0.15, 50

    out: List[Dict[str, Any]] = []
    dropped = 0

    for o in list(orders or [])[: int(max_n)]:
        if not isinstance(o, dict):
            continue
        sym = str(o.get("symbol") or "").strip()
        if not sym:
            continue

        # weight caps (defense in depth; upstream should already manage this)
        to_w = o.get("to_weight")
        try:
            to_wf = float(to_w) if to_w is not None else 0.0
        except Exception:
            to_wf = 0.0
        if abs(to_wf) > float(max_abs_w):
            dropped += 1
            continue

        # delta-weight cap (if present)
        dw = o.get("delta_weight")
        if dw is not None:
            try:
                dwf = float(dw)
                if abs(dwf) > float(max_abs_dw):
                    dropped += 1
                    continue
            except Exception:
                pass

        out.append(o)

    info = {
        "ok": True,
        "status": "governed",
        "broker": broker,
        "mode": mode,
        "in_n": int(len(list(orders or []))),
        "out_n": int(len(out)),
        "dropped_n": int(dropped),
        "equity_usd": (float(equity_usd) if equity_usd is not None else None),
        "max_abs_weight": float(max_abs_w),
        "max_abs_delta_weight": float(max_abs_dw),
        "max_orders_per_pass": int(max_n),
    }
    return out, info
