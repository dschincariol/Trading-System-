# dev_core/trade_attribution_ledger.py

import json
import time
from typing import Any, Dict, Optional

from engine.storage import connect, init_db


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_loads(s: Optional[str]) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


def _pick_model_from_explain(explain: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(explain, dict):
        return {}
    # common patterns in your codebase:
    # - model_name / model_kind / model_ts_ms
    # - model / model_meta nested objects
    m: Dict[str, Any] = {}
    for k in ("model_name", "model_kind", "model_ts_ms", "horizon_s"):
        if k in explain and explain.get(k) is not None:
            m[k] = explain.get(k)
    if "model" in explain and isinstance(explain.get("model"), dict):
        for k, v in (explain.get("model") or {}).items():
            if v is not None:
                m[f"model.{k}"] = v
    if "model_meta" in explain and isinstance(explain.get("model_meta"), dict):
        for k, v in (explain.get("model_meta") or {}).items():
            if v is not None:
                m[f"model_meta.{k}"] = v
    return m


def _pick_regime_vector_from_explain(explain: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(explain, dict):
        return {}
    # common patterns:
    # - regime / current_regime
    # - regime_vector / regime_vec
    out: Dict[str, Any] = {}
    for k in ("regime", "current_regime", "regime_label"):
        if k in explain and explain.get(k) is not None:
            out[k] = explain.get(k)
    for k in ("regime_vector", "regime_vec", "regime_features"):
        if k in explain and isinstance(explain.get(k), dict):
            out[k] = explain.get(k)
    return out


def ensure_trade_attribution_ready() -> None:
    init_db()
    # storage.init_db() now ensures table exists; nothing else required.


def log_suppression(
    *,
    source_alert_id: Optional[int],
    symbol: str,
    suppression_reason: str,
    signal_json: Optional[Dict[str, Any]] = None,
    model_json: Optional[Dict[str, Any]] = None,
    regime_vector_json: Optional[Dict[str, Any]] = None,
    execution_policy_json: Optional[Dict[str, Any]] = None,
    decision_json: Optional[Dict[str, Any]] = None,
) -> None:
    ensure_trade_attribution_ready()
    con = connect(readonly=False)
    try:
        ts = _now_ms()
        con.execute(
            """
            INSERT OR IGNORE INTO trade_attribution_ledger(
              ts_ms, source_alert_id, symbol,
              signal_json, model_json, regime_vector_json,
              execution_policy_json, suppression_reason,
              pnl, fees, slippage_bps,
              decision_json, created_ts_ms
            )
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                int(ts),
                int(source_alert_id) if source_alert_id is not None else None,
                str(symbol or "").strip().upper(),
                json.dumps(signal_json or {}, separators=(",", ":"), sort_keys=True),
                json.dumps(model_json or {}, separators=(",", ":"), sort_keys=True),
                json.dumps(regime_vector_json or {}, separators=(",", ":"), sort_keys=True),
                json.dumps(execution_policy_json or {}, separators=(",", ":"), sort_keys=True),
                str(suppression_reason or "").strip(),
                None,
                None,
                None,
                json.dumps(decision_json or {}, separators=(",", ":"), sort_keys=True),
                int(ts),
            ),
        )
        con.commit()
    finally:
        con.close()


def upsert_from_latest_pnl_attribution_snapshot() -> Dict[str, Any]:
    """
    Reads latest ts_ms from pnl_attribution, enriches with:
      - alerts.explain_json (signal/model/regime hints)
      - execution_policy_audit.decision_json (execution policy decision record)
    Writes rows into trade_attribution_ledger so every $ is explainable.
    """
    ensure_trade_attribution_ready()
    con = connect(readonly=False)
    try:
        r = con.execute("SELECT MAX(ts_ms) FROM pnl_attribution").fetchone()
        pts = int(r[0]) if r and r[0] is not None else None
        if pts is None:
            return {"ok": False, "status": "no_pnl_attribution"}

        rows = con.execute(
            """
            SELECT p.ts_ms, p.source_alert_id, p.symbol, p.pnl, p.fees, p.slippage_bps
            FROM pnl_attribution p
            WHERE p.ts_ms = ?
            """,
            (int(pts),),
        ).fetchall()

        n = 0
        for ts_ms, source_alert_id, symbol, pnl, fees, slippage_bps in rows or []:
            sid = int(source_alert_id)
            sym = str(symbol or "").strip().upper()

            # alerts explain_json -> signal/model/regime
            signal_json: Dict[str, Any] = {"source_alert_id": sid}
            model_json: Dict[str, Any] = {}
            regime_vec: Dict[str, Any] = {}
            try:
                a = con.execute(
                    """
                    SELECT ts_ms, event_title, symbol, horizon_s, expected_z, confidence,
                           severity, rule_id, explain_json
                    FROM alerts
                    WHERE id=?
                    """,
                    (int(sid),),
                ).fetchone()
                if a:
                    ax = _safe_json_loads(a[8]) if a[8] else None
                    signal_json.update(
                        {
                            "alert_ts_ms": int(a[0] or 0),
                            "event_title": str(a[1] or ""),
                            "symbol": str(a[2] or sym),
                            "horizon_s": int(a[3] or 0),
                            "expected_z": float(a[4] or 0.0),
                            "confidence": float(a[5] or 0.0),
                            "severity": str(a[6] or ""),
                            "rule_id": str(a[7] or ""),
                        }
                    )
                    if isinstance(ax, dict):
                        model_json = _pick_model_from_explain(ax)
                        regime_vec = _pick_regime_vector_from_explain(ax)
                        signal_json["alert_explain"] = ax
            except Exception:
                pass

            # latest execution policy audit decision for this alert (if any)
            execution_policy_json: Dict[str, Any] = {}
            decision_json: Dict[str, Any] = {}
            try:
                e = con.execute(
                    """
                    SELECT decision_json
                    FROM execution_policy_audit
                    WHERE source_alert_id=?
                    ORDER BY ts_ms DESC
                    LIMIT 1
                    """,
                    (int(sid),),
                ).fetchone()
                if e and e[0]:
                    dj = _safe_json_loads(e[0])
                    if isinstance(dj, dict):
                        execution_policy_json = dj
                        decision_json = dj
            except Exception:
                pass

            con.execute(
                """
                INSERT OR REPLACE INTO trade_attribution_ledger(
                  ts_ms, source_alert_id, symbol,
                  signal_json, model_json, regime_vector_json,
                  execution_policy_json, suppression_reason,
                  pnl, fees, slippage_bps,
                  decision_json, created_ts_ms
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts_ms),
                    int(sid),
                    sym,
                    json.dumps(signal_json or {}, separators=(",", ":"), sort_keys=True),
                    json.dumps(model_json or {}, separators=(",", ":"), sort_keys=True),
                    json.dumps(regime_vec or {}, separators=(",", ":"), sort_keys=True),
                    json.dumps(execution_policy_json or {}, separators=(",", ":"), sort_keys=True),
                    None,
                    float(pnl or 0.0),
                    float(fees or 0.0),
                    float(slippage_bps) if slippage_bps is not None else None,
                    json.dumps(decision_json or {}, separators=(",", ":"), sort_keys=True),
                    int(_now_ms()),
                ),
            )
            n += 1

        con.commit()
        return {"ok": True, "snapshot_ts_ms": int(pts), "rows_upserted": int(n)}
    finally:
        con.close()


def _ensure_suppression_opportunity_tables(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS suppression_opportunity (
          ts_ms INTEGER NOT NULL,
          ledger_id INTEGER NOT NULL,
          source_alert_id INTEGER,
          symbol TEXT NOT NULL,
          suppression_reason TEXT NOT NULL,

          equity REAL,
          to_weight REAL,
          expected_z REAL,
          confidence REAL,
          volatility REAL,

          expected_alpha_pnl REAL,
          meta_json TEXT,

          PRIMARY KEY (ts_ms, ledger_id)
        );

        CREATE INDEX IF NOT EXISTS idx_supp_opp_ts
          ON suppression_opportunity(ts_ms);

        CREATE INDEX IF NOT EXISTS idx_supp_opp_alert
          ON suppression_opportunity(source_alert_id);

        CREATE INDEX IF NOT EXISTS idx_supp_opp_symbol_ts
          ON suppression_opportunity(symbol, ts_ms);
        """
    )


def _latest_equity(con, ts_ms: int) -> float:
    try:
        # use prior equity snapshot strictly before ts_ms
        r = con.execute(
            """
            SELECT equity
            FROM equity_history
            WHERE ts_ms < ?
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


def _alert_expected_meta(con, alert_id: int) -> Dict[str, Any]:
    """
    Best-effort:
      expected_z, confidence, volatility (from alerts.explain_json)
    """
    out: Dict[str, Any] = {"expected_z": 0.0, "confidence": 0.0, "volatility": 0.0}
    try:
        r = con.execute(
            """
            SELECT expected_z, confidence, explain_json
            FROM alerts
            WHERE id=?
            """,
            (int(alert_id),),
        ).fetchone()
        if not r:
            return out

        out["expected_z"] = float(r[0] or 0.0)
        out["confidence"] = float(r[1] or 0.0)

        ex = _safe_json_loads(r[2]) if r[2] else None
        if isinstance(ex, dict):
            for k in ("volatility", "vol", "sigma", "realized_vol"):
                if k in ex and ex.get(k) is not None:
                    try:
                        out["volatility"] = float(ex.get(k) or 0.0)
                        break
                    except Exception:
                        pass
        return out
    except Exception:
        return out


def suppression_opportunity_snapshot(lookback_ms: int = 86400000) -> Dict[str, Any]:
    """
    Computes counterfactual opportunity cost for suppressed intents:
      expected_alpha_pnl ~= equity * to_weight * (expected_z * volatility)

    Sources:
      - trade_attribution_ledger (suppression_reason != NULL)
      - portfolio_orders (to_weight via source_order_id in signal_json, fallback by source_alert_id)
      - alerts (expected_z/confidence + volatility from explain_json)
      - equity_history (equity at ts_ms)
    Writes into suppression_opportunity.
    """
    ensure_trade_attribution_ready()
    con = connect(readonly=False)
    try:
        _ensure_suppression_opportunity_tables(con)

        now = _now_ms()
        rows = con.execute(
            """
            SELECT id, ts_ms, source_alert_id, symbol, suppression_reason, signal_json
            FROM trade_attribution_ledger
            WHERE suppression_reason IS NOT NULL
              AND ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT 5000
            """,
            (int(now - int(lookback_ms)),),
        ).fetchall() or []

        wrote = 0
        for rid, ts_ms, source_alert_id, symbol, suppression_reason, signal_json in rows:
            ledger_id = int(rid)
            ts_i = int(ts_ms or 0)
            sym = str(symbol or "").strip().upper()
            reason = str(suppression_reason or "").strip()
            sid = int(source_alert_id) if source_alert_id is not None else None

            sig = _safe_json_loads(signal_json) if signal_json else None
            source_order_id = None
            if isinstance(sig, dict) and sig.get("source_order_id") is not None:
                try:
                    source_order_id = int(sig.get("source_order_id"))
                except Exception:
                    source_order_id = None

            to_weight = None

            # 1) Prefer portfolio_orders by id (source_order_id)
            if source_order_id is not None:
                try:
                    po = con.execute(
                        """
                        SELECT to_weight
                        FROM portfolio_orders
                        WHERE id=?
                        """,
                        (int(source_order_id),),
                    ).fetchone()
                    if po and po[0] is not None:
                        to_weight = float(po[0] or 0.0)
                except Exception:
                    pass

            # 2) Fallback: latest portfolio_orders by source_alert_id + symbol
            if to_weight is None and sid is not None:
                try:
                    po = con.execute(
                        """
                        SELECT to_weight
                        FROM portfolio_orders
                        WHERE source_alert_id=? AND symbol=?
                        ORDER BY ts_ms DESC, id DESC
                        LIMIT 1
                        """,
                        (int(sid), sym),
                    ).fetchone()
                    if po and po[0] is not None:
                        to_weight = float(po[0] or 0.0)
                except Exception:
                    pass

            if to_weight is None:
                to_weight = 0.0

            equity = _latest_equity(con, ts_i)

            expected_z = 0.0
            confidence = 0.0
            volatility = 0.0
            if sid is not None:
                em = _alert_expected_meta(con, sid)
                expected_z = float(em.get("expected_z") or 0.0)
                confidence = float(em.get("confidence") or 0.0)
                volatility = float(em.get("volatility") or 0.0)

            expected_ret = float(expected_z) * float(volatility)
            expected_alpha_pnl = float(equity) * float(to_weight) * float(expected_ret)

            meta = {
                "source_order_id": source_order_id,
                "signal_json": sig if isinstance(sig, dict) else None,
            }

            con.execute(
                """
                INSERT OR REPLACE INTO suppression_opportunity(
                  ts_ms, ledger_id, source_alert_id, symbol, suppression_reason,
                  equity, to_weight, expected_z, confidence, volatility,
                  expected_alpha_pnl, meta_json
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(ts_i),
                    int(ledger_id),
                    (int(sid) if sid is not None else None),
                    sym,
                    reason,
                    float(equity),
                    float(to_weight),
                    float(expected_z),
                    float(confidence),
                    float(volatility),
                    float(expected_alpha_pnl),
                    json.dumps(meta, separators=(",", ":"), sort_keys=True),
                ),
            )
            wrote += 1

        con.commit()
        return {"ok": True, "rows_written": int(wrote), "ts_ms": int(now)}
    finally:
        con.close()

def suppression_cost_snapshot(lookback_ms: int = 86400000) -> Dict[str, Any]:
    """
    Measures opportunity cost of suppression:
    compares suppressed signals vs executed pnl.
    """
    con = connect(readonly=True)
    try:
        now = _now_ms()

        executed = con.execute(
            """
            SELECT SUM(COALESCE(pnl,0))
            FROM trade_attribution_ledger
            WHERE suppression_reason IS NULL
              AND ts_ms >= ?
            """,
            (now - int(lookback_ms),),
        ).fetchone()[0]

        suppressed = con.execute(
            """
            SELECT COUNT(1)
            FROM trade_attribution_ledger
            WHERE suppression_reason IS NOT NULL
              AND ts_ms >= ?
            """,
            (now - int(lookback_ms),),
        ).fetchone()[0]

        return {
            "executed_pnl": float(executed or 0.0),
            "suppressed_count": int(suppressed or 0),
            "ts_ms": int(now),
        }
    finally:
        con.close()
