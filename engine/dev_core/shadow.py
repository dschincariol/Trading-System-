# dev_core/shadow.py
import json
import time
from typing import Any, Dict, Optional

from engine.dev_core.storage import connect
from engine.dev_core.trade_attribution_ledger import upsert_from_latest_pnl_attribution_snapshot
from engine.dev_core.kill_switch import execution_allowed
from engine.dev_core.rules_engine import evaluate_rules
from engine.dev_core.costs import estimate_cost
from engine.dev_core.model_registry import get_stage_latest
from engine.dev_core.model_v2 import get_current_regime

def _now_ms() -> int:
    return int(time.time() * 1000)

def log_shadow_prediction(
    *,
    event_id: int,
    symbol: str,
    horizon_s: int,
    predicted_z: float,
    confidence: float,
    model_name: str,
    model_kind: Optional[str],
    model_ts_ms: Optional[int],
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    con = connect()
    try:
        regime = None
        try:
            regime = str(get_current_regime() or "").strip()
        except Exception:
            regime = None

        allow, _, _ = execution_allowed(con=con, symbol=symbol, regime=regime)
        if not allow:
            return

        cost = None
        net = None
        try:
            cost = float(estimate_cost(symbol, horizon_s))
            net = float(predicted_z) - float(cost)
        except Exception:
            pass

        

        con.execute(
            """
            INSERT INTO shadow_predictions
              (ts_ms, event_id, symbol, regime, horizon_s,
               model_name, model_kind, model_ts_ms,
               predicted_z, confidence, cost_est, net_pred_z, extra_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _now_ms(),
                int(event_id),
                str(symbol),
                regime,
                int(horizon_s),
                str(model_name),
                model_kind,
                model_ts_ms,
                float(predicted_z),
                float(confidence),
                cost,
                net,
                json.dumps(extra or {}, separators=(",", ":"), sort_keys=True),
            ),
        )
        con.commit()
    finally:
        con.close()

def shadow_predict(
    *,
    event_id: int,
    symbol: str,
    horizon_s: int,
    features: Any,
) -> None:
    """
    Runs shadow model prediction in parallel to champion.
    NEVER returns a value. NEVER executes trades.
    """
    # Latest shadow model (if any)
    rec = get_stage_latest("shadow", symbol=symbol, horizon_s=horizon_s)
    if not rec:
        return

    try:
        pred_z, conf, meta = rec.predict(features)
    except Exception:
        return

    log_shadow_prediction(
        event_id=event_id,
        symbol=symbol,
        horizon_s=horizon_s,
        predicted_z=float(pred_z),
        confidence=float(conf),
        model_name=rec.model_name,
        model_kind=getattr(rec, "kind", None),
        model_ts_ms=getattr(rec, "trained_ts_ms", None),
        extra={"meta": meta or {}},
    )
