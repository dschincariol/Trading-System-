"""
Execution Policy Engine (Unified + Regime Compatible)

Preserves:
- TTL hard stop
- Half-life alpha decay
- Aggressiveness tiers
- Volatility slicing
- Broker-sim overrides
- Kill switch enforcement
- Structured audit trail
- Strict signal timestamp enforcement

Adds:
- Regime compatibility sizing
- Regime compatibility audit logging
"""

import json
import os
import time
import math
from typing import Any, Dict, List, Optional, Tuple

from dev_core.storage import connect
from dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from dev_core.kill_switch import execution_allowed
from dev_core.execution_mode import get_execution_mode
from dev_core.regime_stack import (
    compute_regime_vector,
    regime_compatibility,
    regime_model_version,
)


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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS execution_policy_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          signal_id TEXT,
          symbol TEXT,
          side TEXT,
          qty REAL,
          age_ms INTEGER,
          ttl_ms INTEGER,
          volatility REAL,
          regime_compat REAL,
          source_order_id INTEGER,
          policy_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_epe_audit_ts ON execution_policy_audit(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_epe_audit_sym ON execution_policy_audit(symbol);
        """
    )


def _alpha_remaining(age_ms: int, half_life_ms: int, ttl_ms: int) -> float:
    if ttl_ms <= 0 or age_ms >= ttl_ms:
        return 0.0
    hl = max(1, half_life_ms)
    rem = math.pow(0.5, float(age_ms) / float(hl))
    ttl_frac = max(0.0, 1.0 - (float(age_ms) / float(ttl_ms)))
    return max(0.0, min(1.0, rem * (0.5 + 0.5 * ttl_frac)))


def _decision_from_alpha(alpha_rem: float):
    if alpha_rem >= PASSIVE_MIN_ALPHA:
        return "LIMIT", "PASSIVE", SIM_LAT_MS_PASSIVE, SIM_CHUNK_PCT_PASSIVE, SIM_EXTRA_SLIP_BPS_PASSIVE
    if alpha_rem >= NEUTRAL_MIN_ALPHA:
        return "LIMIT", "NEUTRAL", SIM_LAT_MS_NEUTRAL, SIM_CHUNK_PCT_NEUTRAL, SIM_EXTRA_SLIP_BPS_NEUTRAL
    return "MARKET", "AGGRESSIVE", SIM_LAT_MS_AGGRESSIVE, SIM_CHUNK_PCT_AGGRESSIVE, SIM_EXTRA_SLIP_BPS_AGGRESSIVE


def apply_execution_policy(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:

    shaped: List[Dict[str, Any]] = []
    con = connect()

    try:
        _ensure_tables(con)

        allow, ks_reason, ks_meta = execution_allowed(con=con, symbol=None, regime=None)
        if not allow:
            return []

        now_ms = _now_ms()

        for o in list(orders or []):
            if not isinstance(o, dict):
                continue

            signal_ts = int(o.get("signal_ts_ms") or 0)
            ttl_ms = int(o.get("alpha_ttl_ms") or DEFAULT_TTL_MS)

            if signal_ts <= 0 and STRICT_SIGNAL_TS:
                continue

            age_ms = now_ms - signal_ts if signal_ts > 0 else 0
            if age_ms > ttl_ms:
                continue

            half_life_ms = int(o.get("alpha_half_life_ms") or DEFAULT_HALF_LIFE_MS)
            alpha_rem = _alpha_remaining(age_ms, half_life_ms, ttl_ms)

            order_type, aggressiveness, sim_lat_ms, sim_chunk_pct, sim_extra_slip = \
                _decision_from_alpha(alpha_rem)

            qty = float(o.get("qty") or 0.0)
            if qty == 0.0:
                continue

            symbol = str(o.get("symbol") or "").strip()
            if not symbol:
                continue

            side = str(o.get("side") or "").upper().strip()
            volatility = float(o.get("volatility") or 0.0)

            # ----------------------------
            # Regime compatibility shaping
            # ----------------------------
            try:
                regime_vec = compute_regime_vector(symbol=symbol, ts_ms=int(signal_ts), con=con)
            except Exception:
                regime_vec = None

            try:
                prof = o.get("regime_profile")
                if isinstance(prof, dict) and regime_vec:
                    regime_comp = float(regime_compatibility(prof, regime_vec))
                else:
                    regime_comp = 1.0
            except Exception:
                regime_comp = 1.0

            if not (regime_comp == regime_comp):
                regime_comp = 1.0

            regime_comp = max(0.0, min(1.0, float(regime_comp)))

            qty = float(qty) * float(regime_comp)
            if qty == 0.0:
                continue

            compat = float(regime_comp)

            # Volatility slicing
            slice_pct = 0.15 if volatility > 0.03 else 0.25
            slice_qty = abs(qty) * slice_pct
            if slice_qty <= 0.0:
                slice_qty = abs(qty)

            slices = max(1, int(abs(qty) // slice_qty))
            slices = max(1, min(25, slices))

            for _ in range(slices):
                shaped.append(
                    {
                        **o,
                        "qty": slice_qty if qty > 0 else -slice_qty,
                        "order_type": order_type,
                        "aggressiveness": aggressiveness,
                        "cancel_replace": True,
                        "max_reprice_attempts": 3,
                        "epe_alpha_remaining": alpha_rem,
                        "regime_compatibility": float(regime_comp),
                        "epe_broker_sim_overrides": {
                            "latency_ms": sim_lat_ms,
                            "chunk_pct": sim_chunk_pct,
                            "extra_slippage_bps": sim_extra_slip,
                        },
                    }
                )

                con.execute(
                    """
                    INSERT INTO execution_policy_audit(
                    ts_ms, signal_id, symbol, side, qty,
                    age_ms, ttl_ms, volatility, regime_compat,
                    source_order_id, policy_json
                    )
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        now_ms,
                        str(o.get("signal_id") or ""),
                        symbol,
                        side,
                        qty,
                        age_ms,
                        ttl_ms,
                        volatility,
                        float(regime_comp),
                        int(o.get("source_order_id") or 0),
                        json.dumps(
                            {
                                "alpha_remaining": alpha_rem,
                                "order_type": order_type,
                                "aggressiveness": aggressiveness,
                                "slice_pct": slice_pct,
                                "slice_qty": slice_qty,
                                "slices": slices,
                                "execution_mode": get_execution_mode(),
                                "regime_model_version": str(regime_model_version()),
                                "regime_compat": float(regime_comp),
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )


        con.commit()
        return shaped

    finally:
        try:
            con.close()
        except Exception:
            pass
