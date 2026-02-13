# dev_core/pnl_decomposition_engine.py

import json
import time
from typing import Any, Dict, Optional, Tuple

from dev_core.storage import connect


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _ensure_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS pnl_decomposition (
          ts_ms INTEGER NOT NULL,
          source_alert_id INTEGER NOT NULL,
          symbol TEXT NOT NULL,

          -- signal/model inputs (best-effort)
          expected_z REAL,
          confidence REAL,
          volatility REAL,
          horizon_s INTEGER,

          -- sizing / exposure
          equity REAL,
          to_weight REAL,
          base_weight_est REAL,
          final_mult REAL,
          notional_est REAL,

          -- realized
          realized_pnl REAL NOT NULL,
          fees REAL NOT NULL,
          slippage_bps REAL,

          -- components (in $)
          exec_cost_pnl REAL,
          alpha_expected_pnl REAL,
          sizing_pnl REAL,
          residual_pnl REAL,

          -- provenance
          meta_json TEXT,

          PRIMARY KEY (ts_ms, source_alert_id, symbol)
        );

        CREATE INDEX IF NOT EXISTS idx_pnl_decomp_ts
          ON pnl_decomposition(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_pnl_decomp_alert
          ON pnl_decomposition(source_alert_id);

        CREATE INDEX IF NOT EXISTS idx_pnl_decomp_symbol_ts
          ON pnl_decomposition(symbol, ts_ms);
        """
    )


def _latest_equity(con, ts_ms: int) -> float:
    try:
        r = con.execute(
            """
            SELECT equity
            FROM equity_history
            WHERE ts_ms <= ?
            ORDER BY ts_ms DESC
            LIMIT 1
            """,
            (int(ts_ms),),
        ).fetchone()
        if not r:
            return 0.0
        return float(r[0] or 0.0)
    except Exception:
        return 0.0


def _alert_signal_meta(con, alert_id: int) -> Tuple[float, float, float, int]:
    """
    Returns: expected_z, confidence, volatility, horizon_s (best-effort)
    """
    expected_z = 0.0
    confidence = 0.0
    volatility = 0.0
    horizon_s = 0

    try:
        r = con.execute(
            """
            SELECT expected_z, confidence, horizon_s, explain_json
            FROM alerts
            WHERE id=?
            """,
            (int(alert_id),),
        ).fetchone()
        if not r:
            return expected_z, confidence, volatility, horizon_s

        expected_z = float(r[0] or 0.0)
        confidence = float(r[1] or 0.0)
        horizon_s = int(r[2] or 0)

        ex = _safe_json_loads(r[3]) if r[3] else None
        if isinstance(ex, dict):
            for k in ("volatility", "vol", "sigma", "realized_vol"):
                if ex.get(k) is not None:
                    try:
                        volatility = float(ex.get(k) or 0.0)
                        break
                    except Exception:
                        pass
    except Exception:
        pass

    return float(expected_z), float(confidence), float(volatility), int(horizon_s)


def _order_exposure_meta(con, ts_ms: int, alert_id: int, symbol: str) -> Dict[str, Any]:
    """
    Aggregate exposure from execution_orders for this (alert_id, symbol).
    Uses:
      - execution_orders.qty/ref_px for notional estimate
      - execution_orders.extra_json for to_weight + exec_regime.final_mult (if present)
    """
    sym = str(symbol or "").strip().upper()
    out: Dict[str, Any] = {
        "notional_est": 0.0,
        "to_weight": None,
        "final_mult": None,
        "base_weight_est": None,
        "n_orders": 0,
    }

    rows = []
    try:
        rows = con.execute(
            """
            SELECT qty, ref_px, extra_json, submit_ts_ms
            FROM execution_orders
            WHERE source_alert_id=? AND symbol=?
            ORDER BY submit_ts_ms DESC
            LIMIT 50
            """,
            (int(alert_id), sym),
        ).fetchall()
    except Exception:
        rows = []

    # Notional estimate from orders (fallback when equity/weights missing)
    notional = 0.0
    last_extra = None
    for qty, ref_px, extra_json, _ in rows or []:
        q = float(qty or 0.0)
        px = float(ref_px or 0.0)
        if px > 0.0:
            notional += abs(q * px)
        if last_extra is None and extra_json:
            last_extra = extra_json

    out["notional_est"] = float(notional)
    out["n_orders"] = int(len(rows or []))

    # Pull sizing meta from last_extra (best-effort)
    exo = _safe_json_loads(last_extra) if isinstance(last_extra, str) else None
    if isinstance(exo, dict):
        if exo.get("to_weight") is not None:
            try:
                out["to_weight"] = float(exo.get("to_weight") or 0.0)
            except Exception:
                pass

        # portfolio_execution_intents attaches exec_regime{final_mult,...}
        reg = exo.get("exec_regime")
        if isinstance(reg, dict) and reg.get("final_mult") is not None:
            try:
                out["final_mult"] = float(reg.get("final_mult") or 0.0)
            except Exception:
                pass

        # If final_mult exists, infer pre-stress base_weight_est
        try:
            tw = out.get("to_weight")
            fm = out.get("final_mult")
            if tw is not None and fm is not None and float(fm) > 1e-12:
                out["base_weight_est"] = float(tw) / float(fm)
        except Exception:
            pass

    return out


def compute_pnl_decomposition_snapshot() -> Dict[str, Any]:
    """
    Decompose realized PnL into:
      - exec_cost_pnl ($): fees + slippage_bps * notional
      - alpha_expected_pnl ($): base_weight_est * equity * (expected_z * volatility)
      - sizing_pnl ($): (to_weight - base_weight_est) * equity * (expected_z * volatility)
      - residual_pnl ($): realized - (alpha_expected + sizing - exec_cost)
    Writes into pnl_decomposition for latest pnl_attribution snapshot ts_ms.
    """
    con = connect(readonly=False)
    try:
        _ensure_tables(con)

        r = con.execute("SELECT MAX(ts_ms) FROM pnl_attribution").fetchone()
        pts = int(r[0]) if r and r[0] is not None else None
        if pts is None:
            return {"ok": False, "status": "no_pnl_attribution"}

        rows = con.execute(
            """
            SELECT ts_ms, source_alert_id, symbol, pnl, fees, slippage_bps, extra_json
            FROM pnl_attribution
            WHERE ts_ms=?
            """,
            (int(pts),),
        ).fetchall()

        n = 0
        for ts_ms, sid, sym, pnl, fees, sl_bps, extra_json in rows or []:
            sid_i = int(sid)
            sym_u = str(sym or "").strip().upper()

            realized_pnl = float(pnl or 0.0)
            fees_f = float(fees or 0.0)
            sl_bps_f = float(sl_bps) if sl_bps is not None else None

            expected_z, confidence, volatility, horizon_s = _alert_signal_meta(con, sid_i)
            equity = _latest_equity(con, int(ts_ms))

            exp = _order_exposure_meta(con, int(ts_ms), sid_i, sym_u)
            to_weight = exp.get("to_weight")
            final_mult = exp.get("final_mult")
            base_weight_est = exp.get("base_weight_est")
            notional_est = float(exp.get("notional_est") or 0.0)

            # cost in $
            exec_cost_pnl = fees_f
            if sl_bps_f is not None and notional_est > 0.0:
                exec_cost_pnl += (float(sl_bps_f) / 10000.0) * float(notional_est)

            # expected return proxy (z * vol)
            expected_ret = float(expected_z) * float(volatility)

            alpha_expected_pnl = None
            sizing_pnl = None

            if equity > 0.0 and base_weight_est is not None:
                alpha_expected_pnl = float(base_weight_est) * float(equity) * float(expected_ret)

                if to_weight is not None:
                    sizing_pnl = (float(to_weight) - float(base_weight_est)) * float(equity) * float(expected_ret)
                else:
                    sizing_pnl = 0.0

            # residual
            model_sum = 0.0
            if alpha_expected_pnl is not None:
                model_sum += float(alpha_expected_pnl)
            if sizing_pnl is not None:
                model_sum += float(sizing_pnl)

            residual = float(realized_pnl) - (model_sum - float(exec_cost_pnl))

            meta = {
                "pnl_attribution_extra": (_safe_json_loads(extra_json) if extra_json else None),
                "exposure_meta": exp,
            }

            con.execute(
                """
                INSERT OR REPLACE INTO pnl_decomposition(
                  ts_ms, source_alert_id, symbol,
                  expected_z, confidence, volatility, horizon_s,
                  equity, to_weight, base_weight_est, final_mult, notional_est,
                  realized_pnl, fees, slippage_bps,
                  exec_cost_pnl, alpha_expected_pnl, sizing_pnl, residual_pnl,
                  meta_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts_ms),
                    int(sid_i),
                    sym_u,
                    float(expected_z),
                    float(confidence),
                    float(volatility),
                    int(horizon_s),
                    float(equity),
                    (float(to_weight) if to_weight is not None else None),
                    (float(base_weight_est) if base_weight_est is not None else None),
                    (float(final_mult) if final_mult is not None else None),
                    float(notional_est),
                    float(realized_pnl),
                    float(fees_f),
                    (float(sl_bps_f) if sl_bps_f is not None else None),
                    float(exec_cost_pnl),
                    (float(alpha_expected_pnl) if alpha_expected_pnl is not None else None),
                    (float(sizing_pnl) if sizing_pnl is not None else None),
                    float(residual),
                    json.dumps(meta, separators=(",", ":"), sort_keys=True),
                ),
            )
            n += 1

        con.commit()
        return {"ok": True, "snapshot_ts_ms": int(pts), "rows_written": int(n)}
    finally:
        con.close()
