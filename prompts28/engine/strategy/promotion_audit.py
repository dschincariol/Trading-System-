# dev_core/promotion_audit.py
import json
import time
from typing import Optional, Dict, Any

from engine.storage import connect, init_db


def _now_ms() -> int:
    return int(time.time() * 1000)


def audit(
    *,
    actor: str,
    action: str,
    model_name: str,
    from_kind: Optional[str] = None,
    from_ts_ms: Optional[int] = None,
    to_kind: Optional[str] = None,
    to_ts_ms: Optional[int] = None,
    reason: Optional[Dict[str, Any]] = None,
    regime: Optional[str] = None,
) -> None:
    init_db()

    rj = dict(reason or {})
    try:
        from engine.strategy.self_critic import snapshot_for_promotion_gate

        rj.setdefault(
            "self_critic",
            snapshot_for_promotion_gate(lookback_ms=6 * 60 * 60 * 1000),
        )
    except Exception:
        pass

    with connect() as con:
        

        con.execute(
            """
            INSERT INTO model_promotion_audit(
              ts_ms, actor, action, model_name,
              from_model_kind, from_model_ts_ms,
              to_model_kind, to_model_ts_ms,
              reason_json,
              regime
            )
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                _now_ms(),
                str(actor),
                str(action),
                str(model_name),
                (str(from_kind) if from_kind else None),
                (int(from_ts_ms) if from_ts_ms is not None else None),
                (str(to_kind) if to_kind else None),
                (int(to_ts_ms) if to_ts_ms is not None else None),
                json.dumps(rj, separators=(",", ":"), sort_keys=True),
                (str(regime) if regime is not None else None),
            ),
        )
        con.commit()
    finally:
        con.close()
