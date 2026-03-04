# dev_core/portfolio_execution_intents.py
"""
Portfolio Execution Intents loader

Purpose:
- Read the *latest* portfolio_orders batch from the existing row-per-order table
- Convert it into a list[dict] that execution layers (EPE / broker_sim / live brokers) can consume
- Attach alpha lifecycle metadata using alerts table when possible (signal_ts_ms / ttl / half-life / volatility)

Does NOT change signal generation. Purely a reader/adapter.
"""

import json
import time
import os
import math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BATCH_WINDOW_MS = 2500  # group "latest run" orders by recent ts_ms window
DEFAULT_SIGNAL_TTL_MS = int(os.environ.get("DEFAULT_SIGNAL_TTL_MS", "1800000"))  # 30m

# -----------------------------
# Execution stress regime (model-aware sizing)
# - options.skew_25d_z
# - flows.index_constituent_imbalance_z
# - earnings proximity via earnings_calendar
# -----------------------------
_EXEC_SKEW_Z_THRESH = float(os.environ.get("EXEC_SKEW_Z_THRESH", "1.5"))
_EXEC_FLOW_Z_THRESH = float(os.environ.get("EXEC_FLOW_Z_THRESH", "2.0"))
_EXEC_STRESS_SIZE_MAX_REDUCTION = float(os.environ.get("EXEC_STRESS_SIZE_MAX_REDUCTION", "0.35"))

_EARNINGS_HALF_LIFE_DAYS = float(os.environ.get("EARNINGS_HALF_LIFE_DAYS", "5.0"))
_EXEC_EARNINGS_SIZE_MAX_REDUCTION = float(os.environ.get("EXEC_EARNINGS_SIZE_MAX_REDUCTION", "0.55"))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _clamp(x: float, lo: float, hi: float) -> float:
    try:
        v = float(x)
    except Exception:
        v = float(lo)
    return float(max(float(lo), min(float(hi), v)))


def _get_factor_feature_asof(con, feature_id: str, ts_ms: int) -> float:
    try:
        # avoid using features stamped exactly at ts_ms to prevent peek-ahead
        q_ts = int(ts_ms) - 1
        row = con.execute(
            """
            SELECT value
            FROM factor_features
            WHERE feature_id=?
              AND asof_ts < ?
              AND effective_ts < ?
            ORDER BY asof_ts DESC, effective_ts DESC
            LIMIT 1
            """,
            (str(feature_id), q_ts, q_ts),
        ).fetchone()
        if not row:
            return 0.0
        return float(row[0]) if row[0] is not None else 0.0
    except Exception:
        return 0.0


def _ymd_from_ts_ms(ts_ms: int) -> str:
    try:
        dt = datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts_ms) / 1000.0))


def _earnings_proximity_decay(con, symbol: str, ts_ms: int) -> float:
    """
    Returns [0,1]. 1.0 = very near earnings date, 0.0 = far.
    Uses nearest earnings_calendar row by date distance.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return 0.0

    try:
        today = _ymd_from_ts_ms(int(ts_ms))
        row = con.execute(
            """
            SELECT earnings_date
            FROM earnings_calendar
            WHERE symbol=?
            ORDER BY ABS(julianday(earnings_date) - julianday(?)) ASC
            LIMIT 1
            """,
            (sym, str(today)),
        ).fetchone()
        if not row:
            return 0.0

        ed = str(row[0] or "").strip()
        if not ed:
            return 0.0

        jd = con.execute(
            "SELECT (julianday(?) - julianday(?))",
            (str(ed), str(today)),
        ).fetchone()
        if not jd or jd[0] is None:
            return 0.0

        days = float(jd[0])
        hl = max(0.5, float(_EARNINGS_HALF_LIFE_DAYS))
        return float(_clamp(math.exp(-abs(days) / hl), 0.0, 1.0))
    except Exception:
        return 0.0


def _portfolio_orders_latest_anchor(con) -> Optional[Tuple[int, int]]:
    row = con.execute(
        """
        SELECT id, ts_ms
        FROM portfolio_orders
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    try:
        return int(row[0]), int(row[1] or 0)
    except Exception:
        return None


def _portfolio_orders_batch_rows(
    con,
    window_ms: int = DEFAULT_BATCH_WINDOW_MS,
    max_rows: int = 5000,
) -> Tuple[Optional[int], Optional[int], List[tuple]]:
    """
    Returns (batch_id, batch_ts_ms, rows)
    batch_id is the max id within the batch window.
    """
    anchor = _portfolio_orders_latest_anchor(con)
    if not anchor:
        return None, None, []

    anchor_id, anchor_ts = anchor
    if anchor_ts <= 0:
        # fallback: use now-ms window keyed by id only
        anchor_ts = _now_ms()

    lo = int(anchor_ts) - int(max(250, int(window_ms)))
    rows = con.execute(
        """
        SELECT
          id, ts_ms, symbol, action,
          from_side, to_side,
          from_weight, to_weight, delta_weight,
          reason, source_alert_id, source_rule_id, explain_json
        FROM portfolio_orders
        WHERE ts_ms >= ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (int(lo), int(max_rows)),
    ).fetchall()

    batch_id = None
    batch_ts = None
    if rows:
        try:
            batch_id = int(max(int(r[0]) for r in rows))
        except Exception:
            batch_id = int(anchor_id)
        try:
            batch_ts = int(max(int(r[1] or 0) for r in rows))
        except Exception:
            batch_ts = int(anchor_ts)

    return batch_id, batch_ts, rows or []


def _alert_meta(con, alert_id: int) -> Dict[str, Any]:
    """
    Best-effort pull from alerts table:
      ts_ms, horizon_s, volatility, alpha_ttl_ms, alpha_half_life_ms
    """
    try:
        r = con.execute(
            """
            SELECT ts_ms, horizon_s, explain_json
            FROM alerts
            WHERE id=?
            """,
            (int(alert_id),),
        ).fetchone()
        if not r:
            return {}
        ts_ms = int(r[0] or 0)
        horizon_s = int(r[1] or 0)
        ex = _safe_json_loads(r[2]) if r[2] else None
        out: Dict[str, Any] = {"signal_ts_ms": ts_ms, "horizon_s": horizon_s}
        if isinstance(ex, dict):
            # common keys seen in explain_json variants
            for k in ("volatility", "vol", "sigma", "realized_vol"):
                if k in ex and ex.get(k) is not None:
                    try:
                        out["volatility"] = float(ex.get(k))
                        break
                    except Exception:
                        pass
            for k in ("alpha_ttl_ms", "ttl_ms"):
                if k in ex and ex.get(k) is not None:
                    try:
                        out["alpha_ttl_ms"] = int(ex.get(k))
                        break
                    except Exception:
                        pass
            for k in ("alpha_half_life_ms", "half_life_ms"):
                if k in ex and ex.get(k) is not None:
                    try:
                        out["alpha_half_life_ms"] = int(ex.get(k))
                        break
                    except Exception:
                        pass
        return out
    except Exception:
        return {}


def _derive_alpha_ttl_ms(horizon_s: int) -> int:
    # Practical defaults: min 20s, max 15m, horizon-driven if available
    try:
        hs = int(horizon_s or 0)
    except Exception:
        hs = 0
    ttl = hs * 1000 if hs > 0 else 5 * 60 * 1000
    ttl = max(20_000, min(15 * 60 * 1000, int(ttl)))
    return int(ttl)


def _derive_half_life_ms(ttl_ms: int) -> int:
    ttl_ms = int(ttl_ms or 0)
    if ttl_ms <= 0:
        return 90_000
    # 1/3 ttl, clamped
    hl = int(ttl_ms // 3)
    hl = max(10_000, min(10 * 60 * 1000, hl))
    return int(hl)


def load_latest_execution_intents(
    con,
    window_ms: int = DEFAULT_BATCH_WINDOW_MS,
    max_rows: int = 5000,
) -> Dict[str, Any]:
    """
    Returns:
      {
        ok: bool,
        batch_id: int|None,
        batch_ts_ms: int|None,
        intents: [dict...]
      }

    Each intent includes:
      symbol, to_side, to_weight, source_alert_id, source_rule_id, explain_json (parsed best-effort),
      plus: signal_ts_ms, alpha_ttl_ms, alpha_half_life_ms, volatility (best-effort)
    """
    batch_id, batch_ts_ms, rows = _portfolio_orders_batch_rows(con, window_ms=window_ms, max_rows=max_rows)
    if not rows:
        return {"ok": True, "batch_id": None, "batch_ts_ms": None, "intents": []}

    intents: List[Dict[str, Any]] = []
    for r in rows:
        try:
            (
                rid, ts_ms, symbol, action,
                from_side, to_side,
                from_w, to_w, delta_w,
                reason, source_alert_id, source_rule_id, explain_json,
            ) = r
        except Exception:
            continue

        sym = str(symbol or "").strip().upper()
        if not sym:
            continue

        ex_obj = _safe_json_loads(explain_json) if explain_json else None

        intent: Dict[str, Any] = {
            "source_order_id": int(rid),
            "ts_ms": int(ts_ms or 0),
            "symbol": sym,
            "action": str(action or ""),
            "from_side": str(from_side or ""),
            "to_side": str(to_side or ""),
            "from_weight": float(from_w or 0.0),
            "to_weight": float(to_w or 0.0),
            "delta_weight": float(delta_w or 0.0),
            "reason": str(reason or ""),
            "source_alert_id": (int(source_alert_id) if source_alert_id is not None else None),
            "source_rule_id": (int(source_rule_id) if source_rule_id is not None else None),
            "explain": (ex_obj if isinstance(ex_obj, dict) else None),
        }

        # attach alpha meta from alert when possible
        a_id = intent.get("source_alert_id")
        if a_id is not None:
            meta = _alert_meta(con, int(a_id))
            sig_ts = int(meta.get("signal_ts_ms") or 0)
            horizon_s = int(meta.get("horizon_s") or 0)

            ttl_ms = int(meta.get("alpha_ttl_ms") or 0)
            if ttl_ms <= 0:
                ttl_ms = _derive_alpha_ttl_ms(horizon_s)

            hl_ms = int(meta.get("alpha_half_life_ms") or 0)
            if hl_ms <= 0:
                hl_ms = _derive_half_life_ms(ttl_ms)

            intent["signal_ts_ms"] = sig_ts if sig_ts > 0 else int(intent.get("ts_ms") or 0)
            intent["alpha_ttl_ms"] = int(ttl_ms)
            intent["alpha_half_life_ms"] = int(hl_ms)

            if "volatility" in meta:
                try:
                    intent["volatility"] = float(meta["volatility"])
                except Exception:
                    pass
        else:
            intent["signal_ts_ms"] = int(intent.get("ts_ms") or 0)

        # ------------------------------------------------------------
        # Model-aware sizing: apply execution stress regime upstream
        # (broker_sim remains microstructure realism; sizing shifts here)
        # ------------------------------------------------------------
        ts_ref = int(intent.get("ts_ms") or batch_ts_ms or 0)
        if ts_ref <= 0:
            ts_ref = _now_ms()

        # base weight (signed as stored in portfolio_orders; preserve sign)
        try:
            base_to_w = float(intent.get("to_weight") or 0.0)
        except Exception:
            base_to_w = 0.0

        skew_z = _get_factor_feature_asof(con, "options.skew_25d_z", int(ts_ref))
        flow_z = _get_factor_feature_asof(con, "flows.index_constituent_imbalance_z", int(ts_ref))

        stress_mag = max(
            0.0,
            max(
                abs(float(skew_z)) - float(_EXEC_SKEW_Z_THRESH),
                abs(float(flow_z)) - float(_EXEC_FLOW_Z_THRESH),
            ),
        )

        stress_mult = 1.0
        if stress_mag > 0.0:
            stress_mult = float(
                _clamp(
                    1.0 - (float(stress_mag) * float(_EXEC_STRESS_SIZE_MAX_REDUCTION)),
                    0.20,
                    1.0,
                )
            )

        earnings_decay = _earnings_proximity_decay(con, sym, int(ts_ref))
        earnings_mult = 1.0
        if float(earnings_decay) > 0.0:
            earnings_mult = float(
                _clamp(
                    1.0 - (float(earnings_decay) * float(_EXEC_EARNINGS_SIZE_MAX_REDUCTION)),
                    0.20,
                    1.0,
                )
            )

        final_mult = float(stress_mult) * float(earnings_mult)
        final_to_w = float(base_to_w) * float(final_mult)

        # ------------------------------------------------------------
        # Regime-aware alpha boost
        # Calm regime => longer alpha persistence
        # Stress regime => shorter alpha persistence
        # ------------------------------------------------------------
        alpha_boost_mult = 1.0

        if stress_mag <= 0.0:
            # reward calm regime (extend TTL modestly)
            alpha_boost_mult = 1.15
        else:
            # penalize stressed regime
            alpha_boost_mult = float(
                _clamp(
                    1.0 - (float(stress_mag) * 0.25),
                    0.60,
                    1.0,
                )
            )

        # Adjust alpha TTL and half-life if present
        try:
            ttl0 = int(intent.get("alpha_ttl_ms") or 0)
            hl0 = int(intent.get("alpha_half_life_ms") or 0)

            if ttl0 > 0:
                intent["alpha_ttl_ms"] = int(float(ttl0) * float(alpha_boost_mult))

            if hl0 > 0:
                intent["alpha_half_life_ms"] = int(float(hl0) * float(alpha_boost_mult))
        except Exception:
            pass

        # update intent weights (and keep delta consistent)
        intent["to_weight"] = float(final_to_w)
        try:
            from_w0 = float(intent.get("from_weight") or 0.0)
        except Exception:
            from_w0 = 0.0
        intent["delta_weight"] = float(final_to_w) - float(from_w0)

        # attach regime context for auditing + downstream attribution
        intent["exec_regime"] = {
            "ts_ref": int(ts_ref),
            "skew_z": float(skew_z),
            "flow_z": float(flow_z),
            "stress_mag": float(stress_mag),
            "stress_mult": float(stress_mult),
            "earnings_decay": float(earnings_decay),
            "earnings_mult": float(earnings_mult),
            "final_mult": float(final_mult),
        }

        intents.append(intent)


    return {"ok": True, "batch_id": batch_id, "batch_ts_ms": batch_ts_ms, "intents": intents}
