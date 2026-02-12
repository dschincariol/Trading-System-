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
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_BATCH_WINDOW_MS = 2500  # group "latest run" orders by recent ts_ms window


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


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

        intents.append(intent)

    return {"ok": True, "batch_id": batch_id, "batch_ts_ms": batch_ts_ms, "intents": intents}
