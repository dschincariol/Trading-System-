# dev_core/execution_policy_engine.py
"""
Execution Policy Engine (EPE)

Sits between portfolio intent generation and broker routing.

Responsibilities:
- Enforce signal TTL (hard wall)
- Use half-life to decay aggressiveness
- Decide order type (LIMIT/MARKET) + aggressiveness tier
- Provide broker_sim microstructure overrides (latency/chunk/slip)
- Produce a full, auditable decision record per original intent
- Integrate with kill-switch (hard) and degrade/failure behavior (fail-closed on TTL)
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
    """
    Returns:
      (order_type, aggressiveness, sim_latency_ms, sim_chunk_pct, sim_extra_slip_bps, limit_offset_bps)
    """
    if alpha_rem >= PASSIVE_MIN_ALPHA:
        return "LIMIT", "PASSIVE", SIM_LAT_MS_PASSIVE, SIM_CHUNK_PCT_PASSIVE, SIM_EXTRA_SLIP_BPS_PASSIVE, 2.0
    if alpha_rem >= NEUTRAL_MIN_ALPHA:
        return "LIMIT", "NEUTRAL", SIM_LAT_MS_NEUTRAL, SIM_CHUNK_PCT_NEUTRAL, SIM_EXTRA_SLIP_BPS_NEUTRAL, 6.0
    return "MARKET", "AGGRESSIVE", SIM_LAT_MS_AGGRESSIVE, SIM_CHUNK_PCT_AGGRESSIVE, SIM_EXTRA_SLIP_BPS_AGGRESSIVE, 18.0


def apply_execution_policy(
    con,
    intents: List[Dict[str, Any]],
    actor: str = None,
    mode: str = None,
    broker: str = None,
    portfolio_orders_batch_id: Optional[int] = None,
    default_signal_ts_ms: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Input:  portfolio execution intents (symbol/to_side/to_weight + alpha fields)
    Output: shaped intents with execution policy fields (no prediction logic)

    HARD GUARANTEE:
      - No output intent will remain if signal age > TTL (fail-closed)
    """
    _ensure_tables(con)

    # Hard kill-switch: if tripped globally, return []
    allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=None)
    if not allow:
        return []

    out: List[Dict[str, Any]] = []
    now = _now_ms()

    for o in list(intents or []):
        if not isinstance(o, dict):
            continue

        symbol = str(o.get("symbol") or "").strip().upper()
        if not symbol:
            continue

        to_side = str(o.get("to_side") or "").upper().strip()
        to_weight = o.get("to_weight")
        try:
            to_weight_f = float(to_weight or 0.0)
        except Exception:
            to_weight_f = 0.0

        # skip pure "no-op" weights
        if abs(to_weight_f) < 1e-12 and to_side == "FLAT":
            continue

        signal_ts = int(o.get("signal_ts_ms") or 0)
        if signal_ts <= 0:
            if default_signal_ts_ms is not None:
                try:
                    signal_ts = int(default_signal_ts_ms)
                except Exception:
                    signal_ts = 0

        if signal_ts <= 0 and STRICT_SIGNAL_TS:
            continue

        ttl_ms = int(o.get("alpha_ttl_ms") or DEFAULT_TTL_MS)
        ttl_ms = max(1, int(ttl_ms))

        half_life_ms = int(o.get("alpha_half_life_ms") or DEFAULT_HALF_LIFE_MS)
        half_life_ms = max(1, int(half_life_ms))

        age_ms = max(0, int(now) - int(signal_ts)) if signal_ts > 0 else 0

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

        order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip, limit_offset_bps = \
            _decision_from_alpha(float(alpha_rem))

        # symbol-level kill switch gate (fail-closed)
        allow_sym, sym_reason, sym_meta = execution_allowed(con=con, symbol=symbol, regime=None)
        if not allow_sym:
            continue

        shaped = {
            **o,
            "symbol": symbol,
            "to_side": to_side,
            "to_weight": float(to_weight_f),

            # EPE outputs (no prediction logic)
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
