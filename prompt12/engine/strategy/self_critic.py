import json
import time
from typing import Any, Dict, List, Optional, Tuple

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


def _sev_rank(sev: str) -> int:
    s = str(sev or "").upper().strip()
    if s == "CRIT":
        return 3
    if s == "WARN":
        return 2
    if s == "INFO":
        return 1
    return 0


def _clamp01(x: float) -> float:
    try:
        v = float(x)
    except Exception:
        return 0.0
    if v != v:
        return 0.0
    return max(0.0, min(1.0, v))


def _extract_confidence_from_signal(signal_json: Dict[str, Any]) -> Optional[float]:
    try:
        v = signal_json.get("confidence")
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _extract_expected_z_from_signal(signal_json: Dict[str, Any]) -> Optional[float]:
    try:
        v = signal_json.get("expected_z")
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _extract_rule_id_from_signal(signal_json: Dict[str, Any]) -> Optional[str]:
    try:
        rid = signal_json.get("rule_id")
        if rid is None:
            return None
        rid = str(rid).strip()
        return rid or None
    except Exception:
        return None


def _extract_model_name(model_json: Dict[str, Any]) -> Optional[str]:
    if not isinstance(model_json, dict):
        return None
    for k in ("model_name", "model"):
        if k in model_json and model_json.get(k) is not None:
            try:
                s = str(model_json.get(k)).strip()
                if s:
                    return s
            except Exception:
                pass
    return None


def _extract_regime_label(regime_vec: Dict[str, Any]) -> Optional[str]:
    if not isinstance(regime_vec, dict):
        return None
    for k in ("regime", "current_regime", "regime_label"):
        if k in regime_vec and regime_vec.get(k) is not None:
            try:
                s = str(regime_vec.get(k)).strip()
                return s or None
            except Exception:
                return None
    return None


def ensure_self_critic_schema(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS self_critic_warnings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts_ms INTEGER NOT NULL,
          severity TEXT NOT NULL,
          category TEXT NOT NULL,
          symbol TEXT,
          source_alert_id INTEGER,
          model_name TEXT,
          regime TEXT,
          title TEXT NOT NULL,
          message TEXT NOT NULL,
          evidence_json TEXT,
          created_ts_ms INTEGER NOT NULL,
          UNIQUE(ts_ms, category, symbol, source_alert_id, model_name, title)
        );

        CREATE INDEX IF NOT EXISTS idx_scw_ts ON self_critic_warnings(ts_ms);
        CREATE INDEX IF NOT EXISTS idx_scw_sev_ts ON self_critic_warnings(severity, ts_ms);
        CREATE INDEX IF NOT EXISTS idx_scw_sym_ts ON self_critic_warnings(symbol, ts_ms);
        """
    )


def _compute_warnings_from_ledger(
    con,
    *,
    lookback_ms: int,
    max_rows: int,
) -> List[Dict[str, Any]]:
    now = _now_ms()
    rows = con.execute(
        """
        SELECT ts_ms, source_alert_id, symbol,
               signal_json, model_json, regime_vector_json,
               execution_policy_json, suppression_reason,
               pnl, fees, slippage_bps, decision_json
        FROM trade_attribution_ledger
        WHERE ts_ms >= ?
        ORDER BY ts_ms DESC
        LIMIT ?
        """,
        (int(now - int(lookback_ms)), int(max_rows)),
    ).fetchall() or []

    warnings: List[Dict[str, Any]] = []

    sym_losses: Dict[str, List[Tuple[int, float, float]]] = {}
    key_losses: Dict[str, List[Tuple[int, float, float]]] = {}

    for (
        ts_ms,
        source_alert_id,
        symbol,
        signal_json_s,
        model_json_s,
        regime_vec_s,
        execution_policy_s,
        suppression_reason,
        pnl,
        fees,
        slippage_bps,
        decision_json_s,
    ) in rows:
        sym = str(symbol or "").strip().upper() or None
        sid = int(source_alert_id) if source_alert_id is not None else None

        sig = _safe_json_loads(signal_json_s) if signal_json_s else None
        sig = sig if isinstance(sig, dict) else {}

        mj = _safe_json_loads(model_json_s) if model_json_s else None
        mj = mj if isinstance(mj, dict) else {}

        rv = _safe_json_loads(regime_vec_s) if regime_vec_s else None
        rv = rv if isinstance(rv, dict) else {}

        ep = _safe_json_loads(execution_policy_s) if execution_policy_s else None
        ep = ep if isinstance(ep, dict) else {}

        model_name = _extract_model_name(mj)
        regime = _extract_regime_label(rv)

        conf = _extract_confidence_from_signal(sig)
        exp_z = _extract_expected_z_from_signal(sig)
        rid = _extract_rule_id_from_signal(sig)

        p = None
        try:
            if pnl is not None:
                p = float(pnl)
        except Exception:
            p = None

        if suppression_reason is None and p is not None and sym:
            sym_losses.setdefault(sym, []).append((int(ts_ms or 0), float(conf or 0.0), float(p)))

        if suppression_reason is None and p is not None and sym and rid:
            k = f"{sym}|{rid}"
            key_losses.setdefault(k, []).append((int(ts_ms or 0), float(conf or 0.0), float(p)))

        if suppression_reason is None and p is not None and conf is not None:
            if conf >= 0.85 and p < 0:
                sev = "WARN"
                if p <= -150:
                    sev = "CRIT"
                warnings.append(
                    {
                        "ts_ms": int(ts_ms or 0),
                        "severity": sev,
                        "category": "confidence_calibration",
                        "symbol": sym,
                        "source_alert_id": sid,
                        "model_name": model_name,
                        "regime": regime,
                        "title": "High confidence loss",
                        "message": f"Trade lost money despite high confidence (conf={float(conf):.3f}, pnl={float(p):.2f}).",
                        "evidence": {
                            "confidence": float(conf),
                            "pnl": float(p),
                            "expected_z": (float(exp_z) if exp_z is not None else None),
                            "rule_id": rid,
                            "fees": (float(fees) if fees is not None else None),
                            "slippage_bps": (float(slippage_bps) if slippage_bps is not None else None),
                            "execution_policy": ep,
                        },
                    }
                )

        try:
            regime_compat = None
            if isinstance(ep, dict):
                if "regime_compat" in ep and ep.get("regime_compat") is not None:
                    regime_compat = float(ep.get("regime_compat"))
                elif "regime_compatibility" in ep and ep.get("regime_compatibility") is not None:
                    regime_compat = float(ep.get("regime_compatibility"))

            if suppression_reason is None and regime_compat is not None:
                rc = _clamp01(regime_compat)
                if rc < 0.5:
                    warnings.append(
                        {
                            "ts_ms": int(ts_ms or 0),
                            "severity": "WARN" if rc >= 0.25 else "CRIT",
                            "category": "regime_alignment",
                            "symbol": sym,
                            "source_alert_id": sid,
                            "model_name": model_name,
                            "regime": regime,
                            "title": "Low regime compatibility execution",
                            "message": f"Execution policy regime compatibility is low (regime_compat={rc:.3f}).",
                            "evidence": {
                                "regime_compat": float(rc),
                                "execution_policy": ep,
                                "regime": regime,
                            },
                        }
                    )
        except Exception:
            pass

    def _tail_losses(seq: List[Tuple[int, float, float]], n: int) -> List[Tuple[int, float, float]]:
        s = list(seq or [])
        s.sort(key=lambda x: x[0], reverse=True)
        return s[:n]

    for sym, seq in (sym_losses or {}).items():
        tail = _tail_losses(seq, 5)
        if len(tail) >= 3:
            losses = [x for x in tail if x[2] < 0]
            if len(losses) >= 3:
                avg_conf = sum(x[1] for x in losses) / float(len(losses))
                total_pnl = sum(x[2] for x in losses)
                sev = "WARN"
                if total_pnl <= -250 or avg_conf >= 0.8:
                    sev = "CRIT"
                warnings.append(
                    {
                        "ts_ms": int(tail[0][0]),
                        "severity": sev,
                        "category": "repeated_loss_pattern",
                        "symbol": sym,
                        "source_alert_id": None,
                        "model_name": None,
                        "regime": None,
                        "title": "Repeated losses on symbol",
                        "message": f"Recent trades show repeated losses for {sym} (losses={len(losses)}/{len(tail)}, total_pnl={total_pnl:.2f}, avg_conf={avg_conf:.3f}).",
                        "evidence": {
                            "sample": [{"ts_ms": int(t), "confidence": float(c), "pnl": float(p)} for (t, c, p) in tail],
                        },
                    }
                )

    for k, seq in (key_losses or {}).items():
        tail = _tail_losses(seq, 5)
        if len(tail) >= 3:
            losses = [x for x in tail if x[2] < 0]
            if len(losses) >= 3:
                sym = k.split("|", 1)[0]
                total_pnl = sum(x[2] for x in losses)
                warnings.append(
                    {
                        "ts_ms": int(tail[0][0]),
                        "severity": "WARN" if total_pnl > -200 else "CRIT",
                        "category": "repeated_loss_pattern",
                        "symbol": sym,
                        "source_alert_id": None,
                        "model_name": None,
                        "regime": None,
                        "title": "Repeated losses on rule",
                        "message": f"Same symbol+rule has repeated losses (key={k}, total_pnl={total_pnl:.2f}).",
                        "evidence": {
                            "key": k,
                            "sample": [{"ts_ms": int(t), "confidence": float(c), "pnl": float(p)} for (t, c, p) in tail],
                        },
                    }
                )

    return warnings


def upsert_warnings(
    *,
    lookback_ms: int = 7 * 24 * 60 * 60 * 1000,
    max_rows: int = 5000,
) -> Dict[str, Any]:
    init_db()
    con = connect(readonly=False)
    try:
        ensure_self_critic_schema(con)
        warnings = _compute_warnings_from_ledger(con, lookback_ms=lookback_ms, max_rows=max_rows)

        wrote = 0
        for w in warnings:
            con.execute(
                """
                INSERT OR IGNORE INTO self_critic_warnings(
                  ts_ms, severity, category, symbol, source_alert_id,
                  model_name, regime, title, message, evidence_json, created_ts_ms
                )
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(w.get("ts_ms") or 0),
                    str(w.get("severity") or "INFO"),
                    str(w.get("category") or "unknown"),
                    (str(w.get("symbol")) if w.get("symbol") is not None else None),
                    (int(w.get("source_alert_id")) if w.get("source_alert_id") is not None else None),
                    (str(w.get("model_name")) if w.get("model_name") is not None else None),
                    (str(w.get("regime")) if w.get("regime") is not None else None),
                    str(w.get("title") or ""),
                    str(w.get("message") or ""),
                    json.dumps(w.get("evidence") or {}, separators=(",", ":"), sort_keys=True),
                    int(_now_ms()),
                ),
            )
            if con.total_changes:
                wrote += 1

        con.commit()
        return {"ok": True, "rows_generated": int(len(warnings)), "rows_inserted": int(wrote), "ts_ms": int(_now_ms())}
    finally:
        con.close()


def get_warnings(
    *,
    min_severity: str = "INFO",
    lookback_ms: int = 24 * 60 * 60 * 1000,
    limit: int = 50,
) -> Dict[str, Any]:
    init_db()
    con = connect(readonly=True)
    try:
        ensure_self_critic_schema(con)
        now = _now_ms()
        min_rank = _sev_rank(min_severity)

        rows = con.execute(
            """
            SELECT id, ts_ms, severity, category, symbol, source_alert_id,
                   model_name, regime, title, message, evidence_json
            FROM self_critic_warnings
            WHERE ts_ms >= ?
            ORDER BY ts_ms DESC
            LIMIT ?
            """,
            (int(now - int(lookback_ms)), int(limit)),
        ).fetchall() or []

        items: List[Dict[str, Any]] = []
        for r in rows:
            sev = str(r[2] or "INFO")
            if _sev_rank(sev) < min_rank:
                continue
            items.append(
                {
                    "id": int(r[0]),
                    "ts_ms": int(r[1] or 0),
                    "severity": sev,
                    "category": str(r[3] or ""),
                    "symbol": (str(r[4]) if r[4] is not None else None),
                    "source_alert_id": (int(r[5]) if r[5] is not None else None),
                    "model_name": (str(r[6]) if r[6] is not None else None),
                    "regime": (str(r[7]) if r[7] is not None else None),
                    "title": str(r[8] or ""),
                    "message": str(r[9] or ""),
                    "evidence": _safe_json_loads(r[10]) if r[10] else {},
                }
            )

        return {"ok": True, "ts_ms": int(_now_ms()), "items": items}
    finally:
        con.close()


def snapshot_for_promotion_gate(
    *,
    lookback_ms: int = 6 * 60 * 60 * 1000,
) -> Dict[str, Any]:
    try:
        upsert_warnings(lookback_ms=lookback_ms, max_rows=5000)
    except Exception:
        pass

    res = get_warnings(min_severity="WARN", lookback_ms=lookback_ms, limit=200)
    items = (res.get("items") or []) if isinstance(res, dict) else []

    counts = {"CRIT": 0, "WARN": 0, "INFO": 0}
    worst = "INFO"
    for it in items:
        sev = str(it.get("severity") or "INFO").upper().strip()
        if sev in counts:
            counts[sev] += 1
        if _sev_rank(sev) > _sev_rank(worst):
            worst = sev

    return {
        "ok": True,
        "ts_ms": int(_now_ms()),
        "lookback_ms": int(lookback_ms),
        "counts": counts,
        "worst_severity": worst,
        "top": items[:10],
    }
