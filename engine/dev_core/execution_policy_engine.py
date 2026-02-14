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

from engine.dev_core.kill_switch import execution_allowed
from engine.dev_core.alpha_lifecycle_engine import (
    ensure_alpha_from_intent,
    alpha_state,
    get_adaptive_half_life_ms,
)

from engine.dev_core.trade_attribution_ledger import log_suppression
from engine.dev_core.risk_state import get_state

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
# Capital Preservation Mode (CPM): execution aggressiveness compression
# ------            -- ------------------------------------------------------
EPE_PRESERVE_FORCE_LIMIT = os.environ.get("EPE_PRESERVE_FORCE_LIMIT", "1") == "1"
EPE_PRESERVE_CAP_TIER = os.environ.get("EPE_PRESERVE_CAP_TIER", "PASSIVE").strip().upper() or "PASSIVE"
EPE_PRESERVE_LAT_MS_MULT = float(os.environ.get("EPE_PRESERVE_LAT_MS_MULT", "1.35"))
EPE_PRESERVE_CHUNK_MULT = float(os.environ.get("EPE_PRESERVE_CHUNK_MULT", "0.70"))
EPE_PRESERVE_EXTRA_SLIP_ADD_BPS = float(os.environ.get("EPE_PRESERVE_EXTRA_SLIP_ADD_BPS", "0.5"))
EPE_PRESERVE_LIMIT_OFFSET_ADD_BPS = float(os.environ.get("EPE_PRESERVE_LIMIT_OFFSET_ADD_BPS", "3.0"))


# urgency boost (feedback into execution urgency)
EPE_URGENCY_ENABLED = os.environ.get("EPE_URGENCY_ENABLED", "1") == "1"
EPE_URGENCY_START_FRAC = float(os.environ.get("EPE_URGENCY_START_FRAC", "0.60"))  # start boosting after 60% of TTL
EPE_URGENCY_MAX_STEP = int(os.environ.get("EPE_URGENCY_MAX_STEP", "1"))  # max tier upgrades (0..2)
EPE_URGENCY_MIN_ALPHA = float(os.environ.get("EPE_URGENCY_MIN_ALPHA", "0.35"))  # only boost if alpha still decent

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

# ----------------------------------------------------------------------
# Trade Suppression Engine (TSE)
# ----------------------------------------------------------------------

TSE_FP_STREAK_HARD = int(os.environ.get("TSE_FP_STREAK_HARD", "5"))
TSE_FP_STREAK_SOFT = int(os.environ.get("TSE_FP_STREAK_SOFT", "3"))

TSE_SLIPPAGE_Z_HARD = float(os.environ.get("TSE_SLIPPAGE_Z_HARD", "3.0"))
TSE_SLIPPAGE_Z_SOFT = float(os.environ.get("TSE_SLIPPAGE_Z_SOFT", "1.8"))

TSE_LATENCY_VAR_HARD = float(os.environ.get("TSE_LATENCY_VAR_HARD", "2.5"))
TSE_LATENCY_VAR_SOFT = float(os.environ.get("TSE_LATENCY_VAR_SOFT", "1.5"))

TSE_SIZE_COMPRESSION_MULT = float(os.environ.get("TSE_SIZE_COMPRESSION_MULT", "0.5"))
TSE_SOFT_THROTTLE_MULT = float(os.environ.get("TSE_SOFT_THROTTLE_MULT", "0.7"))


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

        CREATE TABLE IF NOT EXISTS trade_suppression_state (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          ts_ms INTEGER NOT NULL,
          state TEXT NOT NULL,
          fp_streak INTEGER,
          slippage_z REAL,
          latency_var_z REAL,
          reason_json TEXT
        );
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


def _capital_mode() -> str:
    try:
        return str(get_state("capital_mode", "normal") or "normal")
    except Exception:
        return "normal"


def _apply_capital_preservation_mode(
    order_type: str,
    aggressiveness: str,
    sim_lat_ms: int,
    sim_chunk_pct: float,
    sim_extra_slip: float,
    limit_offset_bps: float,
) -> Tuple[str, str, int, float, float, float, Dict[str, Any]]:

    info: Dict[str, Any] = {"active": 0}

    if _capital_mode() != "preserve":
        return order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps, info

    info["active"] = 1
    info["cap_tier"] = str(EPE_PRESERVE_CAP_TIER)

    # cap aggressiveness tier
    try:
        if _aggr_rank(str(aggressiveness)) > _aggr_rank(str(EPE_PRESERVE_CAP_TIER)):
            order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = _tier_params(str(EPE_PRESERVE_CAP_TIER))
            info["tier_capped"] = 1
    except Exception:
        pass

    # force LIMIT in preserve (best-effort)
    if EPE_PRESERVE_FORCE_LIMIT:
        if str(order_type).upper() != "LIMIT":
            order_type = "LIMIT"
            info["force_limit"] = 1

    # less aggressive microstructure
    try:
        sim_lat_ms = int(max(1, float(sim_lat_ms) * float(EPE_PRESERVE_LAT_MS_MULT)))
    except Exception:
        pass
    try:
        sim_chunk_pct = float(max(0.05, min(1.0, float(sim_chunk_pct) * float(EPE_PRESERVE_CHUNK_MULT))))
    except Exception:
        pass
    try:
        sim_extra_slip = float(sim_extra_slip) + float(EPE_PRESERVE_EXTRA_SLIP_ADD_BPS)
    except Exception:
        pass
    try:
        limit_offset_bps = float(limit_offset_bps) + float(EPE_PRESERVE_LIMIT_OFFSET_ADD_BPS)
    except Exception:
        pass

    return order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps, info


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
        from engine.dev_core.execution_analytics_engine import get_slippage_feedback
        return get_slippage_feedback(con, broker=str(broker or ""))
    except Exception:
        return {}


def _market_stress_snapshot(con, ts_ms: int) -> Dict[str, Any]:
    try:
        from engine.dev_core.market_stress import get_market_stress_snapshot
        return get_market_stress_snapshot(con=con, ts_ms=int(ts_ms)) or {}
    except Exception:
        return {}


def _social_regime_vector(symbol: str, ts_ms: int) -> Dict[str, Any]:
    try:
        from engine.dev_core.social_regime import get_social_regime_vector
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

def _evaluate_trade_suppression(con) -> Dict[str, Any]:
    now = _now_ms()

    fp_streak = 0
    slippage_z = 0.0
    latency_var_z = 0.0

    # --- Bayesian expectancy gating
    expectancy_mean = 0.0
    expectancy_sharpe = 0.0
    try:
        from engine.dev_core.execution_analytics_engine import get_rolling_expectancy_stats
        est = get_rolling_expectancy_stats(con, lookback_n=100)
        expectancy_mean = float(est.get("mean") or 0.0)
        expectancy_sharpe = float(est.get("sharpe") or 0.0)
    except Exception:
        expectancy_mean = 0.0
        expectancy_sharpe = 0.0

    # False positive streak (from execution_quality_job / exec_stats)
    try:
        from engine.dev_core.exec_stats import get_false_positive_streak
        fp_streak = int(get_false_positive_streak(con) or 0)
    except Exception:
        fp_streak = 0

    # Slippage Z-score
    try:
        from engine.dev_core.execution_analytics_engine import get_slippage_zscore
        slippage_z = float(get_slippage_zscore(con) or 0.0)
    except Exception:
        slippage_z = 0.0

    # Latency variance Z-score
    try:
        from engine.dev_core.execution_analytics_engine import get_latency_variance_zscore
        latency_var_z = float(get_latency_variance_zscore(con) or 0.0)
    except Exception:
        latency_var_z = 0.0

    state = "NORMAL"

    # load prior state
    prior_state = "NORMAL"
    try:
        row = con.execute(
            "SELECT state FROM trade_suppression_state WHERE id=1"
        ).fetchone()
        if row and row[0]:
            prior_state = str(row[0])
    except Exception:
        prior_state = "NORMAL"

    # --- Regime-weighted suppression
    regime = None
    regime_conf = 0.0
    try:
        from engine.dev_core.social_regime import get_social_regime_vector
        vec = get_social_regime_vector(symbol=None, ts_ms=_now_ms())
        regime = str((vec or {}).get("regime") or "").upper()
        regime_conf = float((vec or {}).get("regime_conf") or 0.0)
    except Exception:
        regime = None
        regime_conf = 0.0

    regime_multiplier = 1.0

    if regime == "FEAR":
        regime_multiplier = 0.7  # tighten thresholds
    elif regime == "MANIA":
        regime_multiplier = 1.2  # loosen slightly
    elif regime == "CHURN":
        regime_multiplier = 0.9

    # --- Drawdown velocity
    dd_velocity = 0.0
    try:
        from engine.dev_core.drawdown_state import get_drawdown_velocity
        dd_velocity = float(get_drawdown_velocity(con) or 0.0)
    except Exception:
        dd_velocity = 0.0

    # HARD trigger
    hard_trigger = (
        fp_streak >= int(TSE_FP_STREAK_HARD * regime_multiplier)
        or slippage_z >= float(TSE_SLIPPAGE_Z_HARD) * regime_multiplier
        or latency_var_z >= float(TSE_LATENCY_VAR_HARD) * regime_multiplier
        or dd_velocity >= 0.02
        or (expectancy_mean < 0.0 and expectancy_sharpe < -0.5)
    )

    soft_trigger = (
        fp_streak >= TSE_FP_STREAK_SOFT
        or slippage_z >= TSE_SLIPPAGE_Z_SOFT
        or latency_var_z >= TSE_LATENCY_VAR_SOFT
    )

    compress_trigger = (
        slippage_z >= 1.2
        or latency_var_z >= 1.2
    )

    if hard_trigger:
        state = "HARD_BLOCK"

    elif prior_state == "HARD_BLOCK":
        # require full recovery before leaving hard block
        if not soft_trigger and not compress_trigger:
            state = "SOFT_THROTTLE"
        else:
            state = "HARD_BLOCK"

    elif soft_trigger:
        state = "SOFT_THROTTLE"

    elif compress_trigger:
        state = "SIZE_COMPRESSION"

    else:
        state = "NORMAL"

    try:
        con.execute(
            """
            INSERT OR REPLACE INTO trade_suppression_state(
              id, ts_ms, state, fp_streak, slippage_z, latency_var_z, reason_json
            )
            VALUES (1,?,?,?,?,?,?)
            """,
            (
                int(now),
                str(state),
                int(fp_streak),
                float(slippage_z),
                float(latency_var_z),
                json.dumps(
                    {
                        "fp_streak": int(fp_streak),
                        "slippage_z": float(slippage_z),
                        "latency_var_z": float(latency_var_z),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            ),
        )
    except Exception:
        pass

    return {
        "state": state,
        "fp_streak": fp_streak,
        "slippage_z": slippage_z,
        "latency_var_z": latency_var_z,
        "expectancy_mean": float(expectancy_mean),
        "expectancy_sharpe": float(expectancy_sharpe),
        "regime": regime,
        "regime_conf": float(regime_conf),
        "dd_velocity": float(dd_velocity),
    }

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

    # ---- Trade Suppression Engine (TSE)
    tse = _evaluate_trade_suppression(con)
    expectancy_mean = float(tse.get("expectancy_mean") or 0.0)
    expectancy_sharpe = float(tse.get("expectancy_sharpe") or 0.0)
    regime = tse.get("regime")
    regime_conf = float(tse.get("regime_conf") or 0.0)
    dd_velocity = float(tse.get("dd_velocity") or 0.0)

    if tse.get("state") == "HARD_BLOCK":
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
                    int(_now_ms()),
                    (str(actor) if actor is not None else None),
                    (str(mode) if mode is not None else None),
                    (str(broker) if broker is not None else None),
                    str(EPE_POLICY_VERSION),
                    None,
                    None,
                    None,
                    None,
                    None,
                    0.0,
                    None,
                    None,
                    None,
                    None,
                    0.0,
                    json.dumps(
                        {
                            "blocked_by_tse": True,
                            "state": tse.get("state"),
                            "reason": "hard_block",
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            )
        except Exception:
            pass

        try:
            con.commit()
        except Exception:
            pass

        return []

    # ---- Execution risk governor (capital guard / trading state) hard gate
    try:
        from engine.dev_core.capital_guard import update_capital_preservation_mode, trading_allowed as _trading_allowed
        # refresh CPM state (best-effort; never blocks by itself)
        try:
            update_capital_preservation_mode(con=con)
        except Exception:
            pass

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
        from engine.dev_core.drawdown_state import get_current_drawdown
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
            try:
                log_suppression(
                    source_alert_id=(int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                    symbol=str(symbol),
                    suppression_reason="missing_signal_ts",
                    signal_json={
                        "source_order_id": o.get("source_order_id"),
                        "source_alert_id": o.get("source_alert_id"),
                        "signal_ts_ms": int(signal_ts),
                        "strict": True,
                    },
                    regime_vector_json=(o.get("exec_regime") if isinstance(o.get("exec_regime"), dict) else None),
                    decision_json={"blocked": True, "reason": "missing_signal_ts"},
                )
            except Exception:
                pass
            continue

        ttl_ms = max(1, int(o.get("alpha_ttl_ms") or DEFAULT_TTL_MS))
        half_life_ms = max(1, int(o.get("alpha_half_life_ms") or DEFAULT_HALF_LIFE_MS))

        # adaptive half-life (auto-shortening per symbol based on decay distribution)
        try:
            half_life_ms = int(get_adaptive_half_life_ms(con, symbol=str(symbol), default_half_life_ms=int(half_life_ms)))
            half_life_ms = max(1, int(half_life_ms))
        except Exception:
            pass

        age_ms = max(0, now - signal_ts)

        # TTL hard wall (fail-closed)
        if age_ms > ttl_ms:
            try:
                log_suppression(
                    source_alert_id=(int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                    symbol=str(symbol),
                    suppression_reason="ttl_expired",
                    signal_json={
                        "source_order_id": o.get("source_order_id"),
                        "source_alert_id": o.get("source_alert_id"),
                        "signal_ts_ms": int(signal_ts) if signal_ts > 0 else None,
                        "age_ms": int(age_ms),
                        "ttl_ms": int(ttl_ms),
                    },
                    regime_vector_json=(o.get("exec_regime") if isinstance(o.get("exec_regime"), dict) else None),
                    decision_json={"blocked": True, "reason": "ttl_expired"},
                )
            except Exception:
                pass
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

        # urgency boost (feedback loop): as signal ages toward TTL, allow a bounded tier upgrade
        if EPE_URGENCY_ENABLED:
            try:
                if ttl_ms > 0 and float(alpha_rem) >= float(EPE_URGENCY_MIN_ALPHA):
                    frac = float(age_ms) / float(ttl_ms)
                    if frac >= float(EPE_URGENCY_START_FRAC):
                        # 0..1 after start
                        u = (frac - float(EPE_URGENCY_START_FRAC)) / max(1e-9, (1.0 - float(EPE_URGENCY_START_FRAC)))
                        u = max(0.0, min(1.0, float(u)))
                        step = int(round(float(EPE_URGENCY_MAX_STEP) * float(u)))
                        step = max(0, min(2, int(step)))

                        # upgrade aggressiveness tier by step (PASSIVE->NEUTRAL->AGGRESSIVE), bounded
                        if step > 0:
                            cur = _aggr_rank(str(aggressiveness))
                            tgt = max(0, min(2, int(cur) + int(step)))
                            if tgt != cur:
                                if tgt == 0:
                                    order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = _tier_params("PASSIVE")
                                elif tgt == 1:
                                    order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = _tier_params("NEUTRAL")
                                else:
                                    order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = _tier_params("AGGRESSIVE")
            except Exception:
                pass

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

        # Capital Preservation Mode: cap aggressiveness + force passive execution profile
        (
            order_type,
            aggressiveness,
            sim_lat_ms,
            sim_chunk_pct,
            sim_extra_slip,
            limit_offset_bps,
            cap_info,
        ) = _apply_capital_preservation_mode(
            order_type=order_type,
            aggressiveness=aggressiveness,
            sim_lat_ms=sim_lat_ms,
            sim_chunk_pct=sim_chunk_pct,
            sim_extra_slip=sim_extra_slip,
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
            try:
                log_suppression(
                    source_alert_id=(int(o.get("source_alert_id")) if o.get("source_alert_id") is not None else None),
                    symbol=str(symbol),
                    suppression_reason="kill_switch_symbol",
                    signal_json={
                        "source_order_id": o.get("source_order_id"),
                        "source_alert_id": o.get("source_alert_id"),
                    },
                    regime_vector_json=(o.get("exec_regime") if isinstance(o.get("exec_regime"), dict) else None),
                    execution_policy_json={"kill_switch_symbol": {"allow": False, "reason": sym_reason, "meta": sym_meta}},
                    decision_json={"blocked": True, "reason": "kill_switch_symbol", "detail": {"reason": sym_reason, "meta": sym_meta}},
                )
            except Exception:
                pass
            continue
        # ---- Capital Preservation size compression (execution layer)
        try:
            if _capital_mode() == "preserve":
                # execution-level compression (does NOT affect portfolio sizing)
                to_weight_f = float(to_weight_f) * 0.75
        except Exception:
            pass

        # ---- Apply Trade Suppression scaling
        suppression_state = tse.get("state")

        if suppression_state == "SOFT_THROTTLE":
            to_weight_f *= float(TSE_SOFT_THROTTLE_MULT)

        elif suppression_state == "SIZE_COMPRESSION":
            to_weight_f *= float(TSE_SIZE_COMPRESSION_MULT)

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
                                "execution_regime_snapshot": {
                                "stress_score": float(stress_score),
                                "stress_raw": stress,
                                "social_regime": social,
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
                            "kill_switch_global": {"allow": bool(allow), "reason": ks_reason, "meta": ks_meta},
                            "kill_switch_symbol": {"allow": bool(allow_sym), "reason": sym_reason, "meta": sym_meta},
                               "capital_preservation": {
                                "mode": _capital_mode(),
                                "active": 1 if _capital_mode() == "preserve" else 0,
                                "cap_info": cap_info,
                            },
                                "expectancy_mean": float(expectancy_mean),
                                "expectancy_sharpe": float(expectancy_sharpe),
                                "regime": regime,
                                "regime_conf": float(regime_conf),
                                "trade_suppression": {
                                "state": tse.get("state"),
                                "fp_streak": tse.get("fp_streak"),
                                "slippage_z": tse.get("slippage_z"),
                                "latency_var_z": tse.get("latency_var_z"),
                            },
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
