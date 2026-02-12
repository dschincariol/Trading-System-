# dev_core/execution_policy_engine.py
"""
Execution Policy Engine (EPE)

Institutional Unified Version (with risk governor + drawdown scaling + regime-aware tuning)

Responsibilities:
- Enforce signal TTL (hard wall, fail-closed)
- Apply alpha half-life decay
- Determine order type + aggressiveness tier
- Integrate execution analytics slippage feedback
- Apply broker_sim microstructure overrides
- Enforce capital/trading risk governor (hard)
- Enforce global + symbol kill-switch
- Apply drawdown-based aggressiveness scaling (portfolio-level)
- Apply regime-aware microstructure tuning (market stress + social regime)
- Produce auditable decision record
"""

import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from dev_core.kill_switch import execution_allowed
from dev_core.alpha_lifecycle_engine import ensure_alpha_from_intent, alpha_state


# ============================================================
# Defaults / knobs
# ============================================================

DEFAULT_TTL_MS = int(os.environ.get("EPE_DEFAULT_TTL_MS", str(5 * 60 * 1000)))
DEFAULT_HALF_LIFE_MS = int(os.environ.get("EPE_DEFAULT_HALF_LIFE_MS", str(90 * 1000)))

STRICT_SIGNAL_TS = os.environ.get("EPE_STRICT_SIGNAL_TS", "1") == "1"

PASSIVE_MIN_ALPHA = float(os.environ.get("EPE_PASSIVE_MIN_ALPHA", "0.70"))
NEUTRAL_MIN_ALPHA = float(os.environ.get("EPE_NEUTRAL_MIN_ALPHA", "0.40"))

SIM_LAT_MS_PASSIVE = int(os.environ.get("EPE_SIM_LAT_MS_PASSIVE", "220"))
SIM_LAT_MS_NEUTRAL = int(os.environ.get("EPE_SIM_LAT_MS_NEUTRAL", "140"))
SIM_LAT_MS_AGGRESSIVE = int(os.environ.get("EPE_SIM_LAT_MS_AGGRESSIVE", "80"))

SIM_CHUNK_PCT_PASSIVE = float(os.environ.get("EPE_SIM_CHUNK_PCT_PASSIVE", "0.22"))
SIM_CHUNK_PCT_NEUTRAL = float(os.environ.get("EPE_SIM_CHUNK_PCT_NEUTRAL", "0.33"))
SIM_CHUNK_PCT_AGGRESSIVE = float(os.environ.get("EPE_SIM_CHUNK_PCT_AGGRESSIVE", "0.45"))

SIM_EXTRA_SLIP_BPS_PASSIVE = float(os.environ.get("EPE_SIM_EXTRA_SLIP_BPS_PASSIVE", "0.0"))
SIM_EXTRA_SLIP_BPS_NEUTRAL = float(os.environ.get("EPE_SIM_EXTRA_SLIP_BPS_NEUTRAL", "0.5"))
SIM_EXTRA_SLIP_BPS_AGGRESSIVE = float(os.environ.get("EPE_SIM_EXTRA_SLIP_BPS_AGGRESSIVE", "1.5"))

EPE_POLICY_VERSION = os.environ.get("EPE_POLICY_VERSION", "v1").strip() or "v1"

# ------            -- ------------------------------------------------------
# Drawdown-based aggressiveness scaling (portfolio-level)
# ------            -- ------------------------------------------------------
# If dd >= NEUTRAL_AT: cap AGGRESSIVE -> NEUTRAL
EPE_DD_AGGR_NEUTRAL_AT = float(os.environ.get("EPE_DD_AGGR_NEUTRAL_AT", "0.08"))
# If dd >= PASSIVE_AT: cap everything -> PASSIVE
EPE_DD_AGGR_PASSIVE_AT = float(os.environ.get("EPE_DD_AGGR_PASSIVE_AT", "0.15"))
# If dd >= FORCE_LIMIT_AT: force LIMIT order_type (best-effort)
EPE_DD_FORCE_LIMIT_AT = float(os.environ.get("EPE_DD_FORCE_LIMIT_AT", "0.10"))

# ------            -- ------------------------------------------------------
# Regime-aware microstructure tuning (market stress + social regime)
# ------            -- ------------------------------------------------------
# market stress 0..1 -> extra slip multiplier (sim) and limit offset add
EPE_STRESS_EXTRA_SLIP_BPS_MAX = float(os.environ.get("EPE_STRESS_EXTRA_SLIP_BPS_MAX", "2.0"))
EPE_STRESS_LIMIT_OFFSET_BPS_MAX = float(os.environ.get("EPE_STRESS_LIMIT_OFFSET_BPS_MAX", "8.0"))
EPE_STRESS_LAT_MS_ADD_MAX = int(os.environ.get("EPE_STRESS_LAT_MS_ADD_MAX", "120"))
EPE_STRESS_CHUNK_MULT_MIN = float(os.environ.get("EPE_STRESS_CHUNK_MULT_MIN", "0.70"))

# social regime adjustments (additive bps to limit offset; additive bps to extra slip)
EPE_REGIME_LIMIT_OFFSET_BPS_FEAR = float(os.environ.get("EPE_REGIME_LIMIT_OFFSET_BPS_FEAR", "4.0"))
EPE_REGIME_EXTRA_SLIP_BPS_FEAR = float(os.environ.get("EPE_REGIME_EXTRA_SLIP_BPS_FEAR", "1.5"))
EPE_REGIME_LAT_MS_ADD_FEAR = int(os.environ.get("EPE_REGIME_LAT_MS_ADD_FEAR", "80"))
EPE_REGIME_CHUNK_MULT_FEAR = float(os.environ.get("EPE_REGIME_CHUNK_MULT_FEAR", "0.80"))

EPE_REGIME_LIMIT_OFFSET_BPS_CHURN = float(os.environ.get("EPE_REGIME_LIMIT_OFFSET_BPS_CHURN", "2.0"))
EPE_REGIME_EXTRA_SLIP_BPS_CHURN = float(os.environ.get("EPE_REGIME_EXTRA_SLIP_BPS_CHURN", "1.0"))
EPE_REGIME_LAT_MS_ADD_CHURN = int(os.environ.get("EPE_REGIME_LAT_MS_ADD_CHURN", "60"))
EPE_REGIME_CHUNK_MULT_CHURN = float(os.environ.get("EPE_REGIME_CHUNK_MULT_CHURN", "0.85"))

# MANIA: slightly more aggressive microstructure (bounded, and still dd-capped)
EPE_REGIME_LIMIT_OFFSET_BPS_MANIA = float(os.environ.get("EPE_REGIME_LIMIT_OFFSET_BPS_MANIA", "-1.5"))
EPE_REGIME_EXTRA_SLIP_BPS_MANIA = float(os.environ.get("EPE_REGIME_EXTRA_SLIP_BPS_MANIA", "0.0"))
EPE_REGIME_LAT_MS_ADD_MANIA = int(os.environ.get("EPE_REGIME_LAT_MS_ADD_MANIA", "-20"))
EPE_REGIME_CHUNK_MULT_MANIA = float(os.environ.get("EPE_REGIME_CHUNK_MULT_MANIA", "1.05"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_policy_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          actor TEXT,
          mode TEXT,
          broker TEXT,
          policy_version TEXT,
          portfolio_orders_batch_id INTEGER,
          source_order_id INTEGER,
          source_alert_id INTEGER,
          symbol TEXT,
          to_side TEXT,
          to_weight REAL,
          signal_ts_ms INTEGER,
          age_ms INTEGER,
          ttl_ms INTEGER,
          half_life_ms INTEGER,
          alpha_remaining REAL,
          decision_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_epe_audit_ts ON execution_policy_audit(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_epe_audit_sym ON execution_policy_audit(symbol);
        CREATE INDEX IF NOT EXISTS idx_epe_audit_alert ON execution_policy_audit(source_alert_id);
        """
    )


def _decision_from_alpha(alpha_rem: float) -> Tuple[str, str, int, float, float, float]:
    if alpha_rem >= PASSIVE_MIN_ALPHA:
        return "LIMIT", "PASSIVE", SIM_LAT_MS_PASSIVE, SIM_CHUNK_PCT_PASSIVE, SIM_EXTRA_SLIP_BPS_PASSIVE, 2.0
    if alpha_rem >= NEUTRAL_MIN_ALPHA:
        return "LIMIT", "NEUTRAL", SIM_LAT_MS_NEUTRAL, SIM_CHUNK_PCT_NEUTRAL, SIM_EXTRA_SLIP_BPS_NEUTRAL, 6.0
    return "MARKET", "AGGRESSIVE", SIM_LAT_MS_AGGRESSIVE, SIM_CHUNK_PCT_AGGRESSIVE, SIM_EXTRA_SLIP_BPS_AGGRESSIVE, 18.0


def _tier_params(tier: str) -> Tuple[str, str, int, float, float, float]:
    t = str(tier or "").upper().strip()
    if t == "PASSIVE":
        return "LIMIT", "PASSIVE", SIM_LAT_MS_PASSIVE, SIM_CHUNK_PCT_PASSIVE, SIM_EXTRA_SLIP_BPS_PASSIVE, 2.0
    if t == "NEUTRAL":
        return "LIMIT", "NEUTRAL", SIM_LAT_MS_NEUTRAL, SIM_CHUNK_PCT_NEUTRAL, SIM_EXTRA_SLIP_BPS_NEUTRAL, 6.0
    return "MARKET", "AGGRESSIVE", SIM_LAT_MS_AGGRESSIVE, SIM_CHUNK_PCT_AGGRESSIVE, SIM_EXTRA_SLIP_BPS_AGGRESSIVE, 18.0


def _aggr_rank(aggr: str) -> int:
    a = str(aggr or "").upper().strip()
    if a == "PASSIVE":
        return 0
    if a == "NEUTRAL":
        return 1
    return 2  # AGGRESSIVE (default)


def _cap_aggressiveness_by_drawdown(
    *,
    dd: float,
    order_type: str,
    aggressiveness: str,
    sim_lat_ms: int,
    sim_chunk_pct: float,
    sim_extra_slip: float,
    limit_offset_bps: float,
) -> Tuple[str, str, int, float, float, float, Dict[str, Any]]:
    info: Dict[str, Any] = {"dd": float(dd), "cap": None, "forced_limit": False}

    try:
        d = float(dd or 0.0)
    except Exception:
        d = 0.0
    d = max(0.0, min(1.0, d))

    cap = None
    if d >= float(EPE_DD_AGGR_PASSIVE_AT):
        cap = "PASSIVE"
    elif d >= float(EPE_DD_AGGR_NEUTRAL_AT):
        cap = "NEUTRAL"

    forced_limit = bool(d >= float(EPE_DD_FORCE_LIMIT_AT))

    if cap is not None:
        info["cap"] = str(cap)
        # downgrade aggressiveness if above cap
        if _aggr_rank(aggressiveness) > _aggr_rank(cap):
            order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = _tier_params(cap)

    if forced_limit:
        info["forced_limit"] = True
        order_type = "LIMIT"

    return (
        str(order_type),
        str(aggressiveness),
        int(sim_lat_ms),
        float(sim_chunk_pct),
        float(sim_extra_slip),
        float(limit_offset_bps),
        info,
    )


def _get_feedback_map(con, broker: str) -> Dict[str, Dict[str, float]]:
    try:
        from dev_core.execution_analytics_engine import get_slippage_feedback
        return get_slippage_feedback(con, broker=str(broker or ""))
    except Exception:
        return {}


def _market_stress_snapshot(con, ts_ms: int) -> Dict[str, Any]:
    try:
        from dev_core.market_stress import get_market_stress_snapshot
        return get_market_stress_snapshot(con=con, ts_ms=int(ts_ms)) or {}
    except Exception:
        return {}


def _social_regime_vector(symbol: str, ts_ms: int) -> Dict[str, Any]:
    try:
        from dev_core.social_regime import get_social_regime_vector
        return get_social_regime_vector(symbol=str(symbol), ts_ms=int(ts_ms)) or {}
    except Exception:
        return {}


def _apply_regime_microstructure_tuning(
    *,
    stress_score: float,
    social: Dict[str, Any],
    sim_lat_ms: int,
    sim_chunk_pct: float,
    sim_extra_slip_bps: float,
    limit_offset_bps: float,
) -> Tuple[int, float, float, float, Dict[str, Any]]:
    info: Dict[str, Any] = {}

    try:
        s = float(stress_score or 0.0)
    except Exception:
        s = 0.0
    s = max(0.0, min(1.0, s))

    # stress -> conservative tuning
    lat_add = int(round(float(EPE_STRESS_LAT_MS_ADD_MAX) * s))
    off_add = float(EPE_STRESS_LIMIT_OFFSET_BPS_MAX) * s
    slip_add = float(EPE_STRESS_EXTRA_SLIP_BPS_MAX) * s

    chunk_mult = 1.0 - (1.0 - float(EPE_STRESS_CHUNK_MULT_MIN)) * s
    if chunk_mult <= 0.0:
        chunk_mult = float(EPE_STRESS_CHUNK_MULT_MIN)

    info["stress_score"] = float(s)
    info["stress_lat_add_ms"] = int(lat_add)
    info["stress_limit_offset_add_bps"] = float(off_add)
    info["stress_extra_slip_add_bps"] = float(slip_add)
    info["stress_chunk_mult"] = float(chunk_mult)

    sim_lat_ms = int(sim_lat_ms) + int(lat_add)
    limit_offset_bps = float(limit_offset_bps) + float(off_add)
    sim_extra_slip_bps = float(sim_extra_slip_bps) + float(slip_add)
    sim_chunk_pct = float(sim_chunk_pct) * float(chunk_mult)

    # social regime -> additional tuning
    reg = str((social or {}).get("regime") or "").upper().strip()
    reg_conf = float((social or {}).get("regime_conf") or 0.0)

    info["social_regime"] = reg or None
    info["social_regime_conf"] = float(reg_conf)

    if reg == "FEAR":
        limit_offset_bps += float(EPE_REGIME_LIMIT_OFFSET_BPS_FEAR) * float(reg_conf)
        sim_extra_slip_bps += float(EPE_REGIME_EXTRA_SLIP_BPS_FEAR) * float(reg_conf)
        sim_lat_ms += int(round(float(EPE_REGIME_LAT_MS_ADD_FEAR) * float(reg_conf)))
        sim_chunk_pct *= float(EPE_REGIME_CHUNK_MULT_FEAR) ** float(reg_conf)

        info["regime_limit_offset_add_bps"] = float(EPE_REGIME_LIMIT_OFFSET_BPS_FEAR) * float(reg_conf)
        info["regime_extra_slip_add_bps"] = float(EPE_REGIME_EXTRA_SLIP_BPS_FEAR) * float(reg_conf)

    elif reg == "CHURN":
        limit_offset_bps += float(EPE_REGIME_LIMIT_OFFSET_BPS_CHURN) * float(reg_conf)
        sim_extra_slip_bps += float(EPE_REGIME_EXTRA_SLIP_BPS_CHURN) * float(reg_conf)
        sim_lat_ms += int(round(float(EPE_REGIME_LAT_MS_ADD_CHURN) * float(reg_conf)))
        sim_chunk_pct *= float(EPE_REGIME_CHUNK_MULT_CHURN) ** float(reg_conf)

        info["regime_limit_offset_add_bps"] = float(EPE_REGIME_LIMIT_OFFSET_BPS_CHURN) * float(reg_conf)
        info["regime_extra_slip_add_bps"] = float(EPE_REGIME_EXTRA_SLIP_BPS_CHURN) * float(reg_conf)

    elif reg == "MANIA":
        limit_offset_bps += float(EPE_REGIME_LIMIT_OFFSET_BPS_MANIA) * float(reg_conf)
        sim_extra_slip_bps += float(EPE_REGIME_EXTRA_SLIP_BPS_MANIA) * float(reg_conf)
        sim_lat_ms += int(round(float(EPE_REGIME_LAT_MS_ADD_MANIA) * float(reg_conf)))
        sim_chunk_pct *= float(EPE_REGIME_CHUNK_MULT_MANIA) ** float(reg_conf)

        info["regime_limit_offset_add_bps"] = float(EPE_REGIME_LIMIT_OFFSET_BPS_MANIA) * float(reg_conf)
        info["regime_extra_slip_add_bps"] = float(EPE_REGIME_EXTRA_SLIP_BPS_MANIA) * float(reg_conf)

    # clamp
    if sim_lat_ms < 0:
        sim_lat_ms = 0
    sim_chunk_pct = max(0.01, min(1.0, float(sim_chunk_pct)))
    sim_extra_slip_bps = max(0.0, float(sim_extra_slip_bps))
    limit_offset_bps = max(0.0, float(limit_offset_bps))

    return int(sim_lat_ms), float(sim_chunk_pct), float(sim_extra_slip_bps), float(limit_offset_bps), info


def apply_execution_policy(
    con,
    intents: List[Dict[str, Any]],
    actor: str = None,
    mode: str = None,
    broker: str = None,
    portfolio_orders_batch_id: Optional[int] = None,
    default_signal_ts_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:

    _ensure_tables(con)

    # ---- Execution risk governor (capital guard / trading state) hard gate
    try:
        from dev_core.capital_guard import trading_allowed as _trading_allowed
        if not bool(_trading_allowed(con=con)):
            return []
    except Exception:
        # best-effort: if capital_guard unavailable, do not block here
        pass

    # ---- Global kill-switch: hard gate
    allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=None)
    if not allow:
        return []

    # ---- Portfolio drawdown snapshot (used for aggressiveness scaling)
    dd = 0.0
    try:
        from dev_core.drawdown_state import get_current_drawdown
        dd = float(get_current_drawdown(con))
    except Exception:
        dd = 0.0
    dd = max(0.0, min(1.0, float(dd)))

    # ---- Market stress snapshot (computed once per batch)
    now = _now_ms()
    stress = _market_stress_snapshot(con, ts_ms=int(now))
    try:
        stress_score = float(stress.get("stress_score", 0.0) or 0.0)
    except Exception:
        stress_score = 0.0
    stress_score = max(0.0, min(1.0, float(stress_score)))

    # ---- Slippage feedback map (analytics loop)
    feedback = _get_feedback_map(con, broker=str(broker or "sim"))

    out: List[Dict[str, Any]] = []

    for o in list(intents or []):
        if not isinstance(o, dict):
            continue

        symbol = str(o.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        to_side = str(o.get("to_side") or "").upper().strip()
        try:
            to_weight_f = float(o.get("to_weight") or 0.0)
        except Exception:
            to_weight_f = 0.0

        if abs(to_weight_f) < 1e-12 and to_side == "FLAT":
            continue

        signal_ts = int(o.get("signal_ts_ms") or 0)
        if signal_ts <= 0 and default_signal_ts_ms is not None:
            try:
                signal_ts = int(default_signal_ts_ms or 0)
            except Exception:
                signal_ts = 0

        if signal_ts <= 0 and STRICT_SIGNAL_TS:
            continue

        ttl_ms = max(1, int(o.get("alpha_ttl_ms") or DEFAULT_TTL_MS))
        half_life_ms = max(1, int(o.get("alpha_half_life_ms") or DEFAULT_HALF_LIFE_MS))

        age_ms = max(0, now - signal_ts)

        # TTL hard wall (fail-closed)
        if age_ms > ttl_ms:
            continue

        # ALE: ensure lifecycle record exists + compute alpha_remaining
        try:
            ensure_alpha_from_intent(con, o)
        except Exception:
            pass

        alpha_rem = None
        try:
            a_id = o.get("source_alert_id")
            if a_id is not None:
                st = alpha_state(con, int(a_id), now_ms=now)
                if st.get("ok") and st.get("exists"):
                    alpha_rem = float(st.get("alpha_remaining") or 0.0)
        except Exception:
            alpha_rem = None

        if alpha_rem is None:
            # fallback: pure half-life decay clipped by ttl
            try:
                rem_hl = math.pow(0.5, float(age_ms) / float(max(1, half_life_ms)))
                ttl_frac = max(0.0, 1.0 - (float(age_ms) / float(max(1, ttl_ms))))
                alpha_rem = max(0.0, min(1.0, rem_hl * (0.5 + 0.5 * ttl_frac)))
            except Exception:
                alpha_rem = 0.0

        # Base decision from alpha
        order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = \
            _decision_from_alpha(float(alpha_rem))

        # Drawdown-based aggressiveness scaling (portfolio-level)
        (
            order_type,
            aggressiveness,
            sim_lat_ms,
            sim_chunk_pct,
            sim_extra_slip,
            limit_offset_bps,
            dd_info,
        ) = _cap_aggressiveness_by_drawdown(
            dd=dd,
            order_type=order_type,
            aggressiveness=aggressiveness,
            sim_lat_ms=sim_lat_ms,
            sim_chunk_pct=sim_chunk_pct,
            sim_extra_slip=sim_extra_slip,
            limit_offset_bps=limit_offset_bps,
        )

        # Regime-aware microstructure tuning (market stress + per-symbol social regime)
        social = _social_regime_vector(symbol=str(symbol), ts_ms=int(now))
        sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps, reg_info = _apply_regime_microstructure_tuning(
            stress_score=stress_score,
            social=social,
            sim_lat_ms=sim_lat_ms,
            sim_chunk_pct=sim_chunk_pct,
            sim_extra_slip_bps=sim_extra_slip,
            limit_offset_bps=limit_offset_bps,
        )

        # Slippage feedback adjustments (analytics loop)
        fb_key = f"{order_type}|{aggressiveness}"
        fb_row = feedback.get(fb_key) or {}
        try:
            limit_offset_bps += float(fb_row.get("limit_offset_bps", 0.0) or 0.0)
        except Exception:
            pass
        try:
            sim_extra_slip += float(fb_row.get("extra_slip_bps", 0.0) or 0.0)
        except Exception:
            pass

        # Symbol-level kill switch gate (fail-closed)
        allow_sym, sym_reason, sym_meta = execution_allowed(con=con, symbol=symbol, regime=None)
        if not allow_sym:
            continue

        shaped = {
            **o,
            "symbol": symbol,
            "to_side": to_side,
            "to_weight": float(to_weight_f),

            "epe_policy_version": str(EPE_POLICY_VERSION),
            "epe_order_type": str(order_type),
            "epe_aggressiveness": str(aggressiveness),
            "epe_alpha_remaining": float(alpha_rem),
            "epe_signal_age_ms": int(age_ms),
            "epe_signal_ttl_ms": int(ttl_ms),
            "epe_signal_ts_ms": int(signal_ts),

            # limit assist for live brokers (best-effort)
            "epe_limit_offset_bps": float(limit_offset_bps),

            # broker_sim microstructure overrides (consumed by broker_sim only)
            "epe_broker_sim_overrides": {
                "latency_ms": int(sim_lat_ms),
                "chunk_pct": float(sim_chunk_pct),
                "extra_slippage_bps": float(sim_extra_slip),
            },

            # cancel/replace contract (adapter may implement)
            "epe_cancel_replace": True,
            "epe_max_reprice_attempts": 3,
        }

        # audit row per intent
        try:
            con.execute(
                """
                INSERT INTO execution_policy_audit(
                  ts_ms, actor, mode, broker, policy_version,
                  portfolio_orders_batch_id, source_order_id, source_alert_id,
                  symbol, to_side, to_weight,
                  signal_ts_ms, age_ms, ttl_ms, half_life_ms, alpha_remaining,
                  decision_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(now),
                    (str(actor) if actor is not None else None),
                    (str(mode) if mode is not None else None),
                    (str(broker) if broker is not None else None),
                    str(EPE_POLICY_VERSION),
                    (int(portfolio_orders_batch_id) if portfolio_orders_batch_id is not None else None),
                    (int(o.get("source_order_id")) if o.get("source_order_id") is not None else None),
                    (int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                    symbol,
                    to_side,
                    float(to_weight_f),
                    int(signal_ts) if signal_ts > 0 else None,
                    int(age_ms),
                    int(ttl_ms),
                    int(half_life_ms),
                    float(alpha_rem),
                    json.dumps(
                        {
                            "order_type": order_type,
                            "aggressiveness": aggressiveness,
                            "limit_offset_bps": float(limit_offset_bps),
                            "sim_overrides": shaped.get("epe_broker_sim_overrides"),
                            "drawdown": float(dd),
                            "dd_scaling": dd_info,
                            "market_stress": {
                                "stress_score": float(stress_score),
                                "vix": float(stress.get("vix", 0.0) or 0.0),
                                "vvix": float(stress.get("vvix", 0.0) or 0.0),
                                "move": float(stress.get("move", 0.0) or 0.0),
                            },
                            "social_regime": {
                                "regime": (social or {}).get("regime"),
                                "regime_conf": float((social or {}).get("regime_conf", 0.0) or 0.0),
                                "mania_score": float((social or {}).get("mania_score", 0.0) or 0.0),
                                "fear_score": float((social or {}).get("fear_score", 0.0) or 0.0),
                                "churn_score": float((social or {}).get("churn_score", 0.0) or 0.0),
                            },
                            "regime_microstructure": reg_info,
                            "feedback": {
                                "key": fb_key,
                                "limit_offset_bps": float(fb_row.get("limit_offset_bps", 0.0) or 0.0),
                                "extra_slip_bps": float(fb_row.get("extra_slip_bps", 0.0) or 0.0),
                            },
                            "kill_switch_global": {"allow": True, "reason": ks_reason, "meta": ks_meta},
                            "kill_switch_symbol": {"allow": True, "reason": sym_reason, "meta": sym_meta},
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
        except Exception:
            pass

        out.append(shaped)

    return out
